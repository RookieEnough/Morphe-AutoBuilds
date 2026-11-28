import re
import json
import time
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote
from src import session

base_url = "https://www.apkmirror.com"
_blocked_by_cloudflare = False
_use_trawl = False
_main_page_cache: dict[str, tuple[int, bytes]] = {}


class ApkMirrorBlocked(RuntimeError):
    """APKMirror declined this runner before it served an application page."""


def _app_slug_candidates(config: dict) -> list[str]:
    """Return the small set of valid-looking APKMirror app slugs to try.

    On APKMirror the publisher slug and app slug are sometimes identical
    (for example ``/apk/pinterest/pinterest/``), while a human-readable app
    title can be much longer.  Trying the publisher as a final fallback fixes
    those genuine 404s without a site-wide search or browser automation.
    """
    candidates = [
        config.get("app_slug"),
        config.get("name"),
        config.get("org"),
    ]
    return list(dict.fromkeys(slug for slug in candidates if slug))


def _cf_get(url, **kwargs):
    """Fetch with Cloudflare handling via the CI browser service.

    When APKMirror serves a Turnstile challenge, route the request through
    trawl (the browser-rendering service already in CI). Once a challenge is
    seen, stay in trawl mode for the rest of the build instead of abandoning
    APKMirror: cookies from the rendered page are reused, and trawl is used
    again if a later request gets challenged.
    """
    global _blocked_by_cloudflare, _use_trawl
    if _blocked_by_cloudflare:
        raise ApkMirrorBlocked("APKMirror blocked this runner earlier in the build")

    kwargs.setdefault("timeout", 20)

    def _via_trawl():
        """Render the page through trawl and sync cookies to the session."""
        from src import trawl

        rendered = trawl.fetch(url)
        if rendered:
            for name, value in rendered.cookies.items():
                session.cookies.set(name, value, domain=".apkmirror.com")
            logging.info("APKMirror page obtained through the CI browser service")
            return rendered
        return None

    def _is_challenge(response) -> bool:
        if response.status_code != 403:
            return False
        body = response.text[:2000].lower()
        return response.headers.get("cf-mitigated") == "challenge" or "cloudflare" in body

    # If a previous request needed trawl, try direct first (cookies may hold),
    # then fall back to trawl without giving up on APKMirror.
    if _use_trawl:
        response = session.get(url, **kwargs)
        if not _is_challenge(response):
            return response
        try:
            rendered = _via_trawl()
            if rendered:
                return rendered
        except Exception as exc:
            logging.debug("APKMirror browser-service retry failed: %s", exc)

    response = session.get(url, **kwargs)
    for backoff in (5, 10):
        if response.status_code != 429:
            break
        delay = min(max(int(response.headers.get("Retry-After") or backoff), backoff), 15)
        logging.info(f"APKMirror rate-limited (429) on {url}; retrying in {delay}s")
        time.sleep(delay)
        response = session.get(url, **kwargs)
    if response.status_code == 429:
        _blocked_by_cloudflare = True
        logging.warning("APKMirror persisted in rate-limiting (429); skipping APKMirror for this build")
        raise ApkMirrorBlocked("APKMirror rate-limited (429)")
    if _is_challenge(response):
        # CI jobs provide Trawl, the same browser-rendering service used by
        # rvb.  It supplies ordinary page HTML/cookies; if it is absent or
        # cannot obtain a page, preserve the existing provider fallback.
        try:
            rendered = _via_trawl()
            if rendered:
                _use_trawl = True
                return rendered
        except Exception as exc:
            logging.debug("APKMirror browser-service fallback failed: %s", exc)
        _blocked_by_cloudflare = True
        logging.warning(
            "APKMirror served a Cloudflare challenge; skipping APKMirror "
            "for this build instead of launching a browser."
        )
        raise ApkMirrorBlocked("APKMirror Cloudflare challenge")
    return response

def extract_base_version(version: str) -> str:
    """Extract base version from CLI version string which may include variant.
    
    E.g., "18.0.3.954559732-release-arm64-v8a" -> "18.0.3.954559732"
          "351.11 - Stable" -> "351.11"
    """
    cleaned = (version or "").strip()
    m = re.match(
        r'^(\d[\d.]*\d(?:\(\d+\)|\s+build\s+\d+)?)(?:\s*-\s*(?:stable|release|beta|alpha|arm64|armeabi|universal|x86|noarch|dpi).*)?$',
        cleaned,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # Fallback: split on known variant markers
    for marker in [' - stable', '-release-', '-beta-', '-alpha-', '-arm64', '-armeabi', '-universal-', '-x86', '-noarch']:
        if marker in cleaned.lower():
            idx = cleaned.lower().find(marker)
            return cleaned[:idx].strip()
    return cleaned


def _get_main_page(main_url: str) -> tuple[int, bytes]:
    """Fetch an app main page once per get_download_link invocation."""
    if main_url in _main_page_cache:
        return _main_page_cache[main_url]
    response = _cf_get(main_url)
    result = (response.status_code, response.content)
    _main_page_cache[main_url] = result
    return result


def get_build_number_for_version(version: str, config: dict) -> tuple[str | None, str]:
    """Fetch build number for a specific version from APKMirror.
    Returns (build_number, format_type) where format_type is 'parentheses' or 'build_suffix'.
    Returns the LOWEST build number found, since patches are typically made for initial builds."""
    try:
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        status, content = _get_main_page(main_url)
        if status == 200:
            soup = BeautifulSoup(content, "html.parser")
            # Collect all build numbers for this version
            builds_found = []
            for link in soup.find_all('a', href=True):
                text = link.get_text()
                if version in text:
                    # Format 1: "32.30.0(1575420)" -> parentheses
                    build_match = re.search(rf'{re.escape(version)}\((\d+)\)', text)
                    if build_match:
                        builds_found.append((build_match.group(1), 'parentheses'))
                    # Format 2: "6.6 build 006" -> build suffix
                    build_match = re.search(rf'{re.escape(version)}\s+build\s+(\d+)', text, re.IGNORECASE)
                    if build_match:
                        builds_found.append((build_match.group(1), 'build_suffix'))
            
            # Return the lowest build number (patches are typically for initial builds)
            if builds_found:
                # Sort by build number (as integer) and return the lowest
                builds_found.sort(key=lambda x: int(x[0]))
                return builds_found[0]
    except Exception as e:
        logging.debug(f"Could not fetch build number: {e}")
    return None, None

def discover_app_main_url(config: dict) -> str | None:
    """Use APKMirror's search endpoint to discover the correct main app page URL when
    the configured 'org/name' combination doesn't match APKMirror's actual URL slugs.
    
    For example, config has org='duolingo', name='duolingo' but the actual page is at
    /apk/duolingo/duolingo-duolingo/. This function searches APKMirror and finds the
    correct main page URL by matching the org and the package name (most reliable).
    
    Returns the full main page URL if found, or None if discovery fails."""
    try:
        org = config.get('org', '')
        name = config.get('name', '')
        package = config.get('package', '')
        
        # Build search query - use package name if available (most precise), else app name
        # Strip ".apk" or trailing dashes from name for cleaner search
        query_terms = []
        if package:
            query_terms.append(package)
        if name:
            query_terms.append(name.replace('-', ' '))
        
        for query in query_terms:
            search_url = f"{base_url}/?post_type=app_release&searchtype=app&s={quote(query)}"
            logging.info(f"Searching APKMirror for app: {search_url}")
            
            try:
                response = _cf_get(search_url)
                if response.status_code != 200:
                    continue
                
                soup = BeautifulSoup(response.content, "html.parser")
                
                # Find all /apk/{org}/{slug}/ links - these are candidate main app pages
                # We prioritize matches under the same 'org' as the config
                found_links = set()
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    # Match pattern /apk/{org}/{slug}/ but NOT /apk/{org}/{slug}/{anything-else}
                    m = re.match(r'^(/apk/[a-z0-9._-]+/[a-z0-9._-]+/)$', href)
                    if m:
                        found_links.add(m.group(1))
                
                if not found_links:
                    continue
                
                # Prefer links under the configured org
                org_links = [link for link in found_links if link.startswith(f"/apk/{org}/")]
                
                # Among org-matching links, find the one most likely to be the right app
                # Strategy: pick one whose slug contains the configured name as a substring
                # If multiple, prefer the shorter slug (more "exact" match)
                candidates = org_links if org_links else list(found_links)
                
                # Filter candidates: prefer those containing 'name' in the slug
                name_matches = [link for link in candidates if name and name in link]
                if name_matches:
                    candidates = name_matches
                
                # Sort by slug length (shorter = more specific match)
                candidates.sort(key=lambda x: len(x))
                
                if candidates:
                    discovered = base_url + candidates[0]
                    logging.info(f"✓ Discovered main app page via search: {discovered}")
                    return discovered
            except ApkMirrorBlocked:
                raise
            except Exception as e:
                logging.debug(f"Error during search query '{query}': {e}")
                continue
        
        logging.debug("No matching app found via search")
        return None
        
    except Exception as e:
        logging.debug(f"Error in discover_app_main_url: {e}")
        return None

def _scrape_release_url_from_soup(
    soup,
    version: str,
    config: dict,
    build_number: str = None,
    build_format: str = None,
    allow_prerelease: bool = False,
    strict_app: bool = False,
) -> str | None:
    """Scan a BeautifulSoup-parsed APKMirror page for a release link matching the version.
    Returns the full release page URL if found, else None."""
    # Generate version variations (original + stripped of leading zeros like 26.04.05 -> 26.4.5)
    version_variants = [version]
    clean_v = ".".join(str(int(p)) if p.isdigit() else p for p in version.split('.'))
    if clean_v != version:
        version_variants.append(clean_v)

    app_slug = (config.get('name') or config.get('app_slug') or '').lower()
    org_slug = (config.get('org') or '').lower()
    release_prefix = (config.get('release_prefix') or '').lower()
    exclude = (config.get('exclude_variant') or '').lower()
    want_wear = "wear" in app_slug or "wear" in release_prefix
    want_tv = any(t in app_slug or t in release_prefix for t in ("android-tv", "-tv"))

    for v in version_variants:
        version_parts = v.split('.')
        # Match full exact version; only allow trimming a trailing '.0' (e.g. 2.5.0 -> 2.5),
        # never strip non-zero patch digits (6.7.9 -> 6.7 would falsely match 6.7.8).
        depths = [len(version_parts)]
        if len(version_parts) >= 3 and version_parts[-1] == '0':
            depths.append(len(version_parts) - 1)

        for i in depths:
            current_ver = ".".join(version_parts[:i])
            current_ver_dash = "-".join(version_parts[:i])

            candidates = []
            for link in soup.find_all('a', href=True):
                href = link['href'].lower()
                if not href.startswith('/apk/'):
                    continue
                # Prevent sibling Wear OS / Android TV variants from matching phone apps
                if not want_wear and re.search(r'(?:^|[/-])wear-os(?:[/-]|$)', href):
                    continue
                if not want_tv and re.search(r'(?:^|[/-])android-tv(?:[/-]|$)', href):
                    continue
                # Ensure the link belongs to this app (prevents matching sidebar popular links)
                exact_app_match = (
                    (app_slug and f"/{app_slug}/" in href)
                    or (release_prefix and f"/{release_prefix}-" in href)
                )
                belongs = exact_app_match or (
                    not strict_app and org_slug and f"/apk/{org_slug}/" in href
                )
                if (app_slug or org_slug or release_prefix) and not belongs:
                    continue

                if exclude and exclude in href:
                    continue
                if not allow_prerelease and re.search(r'(?:^|[/-])(?:beta|alpha)(?:[/-]|$)', href):
                    continue

                # Match bounded version, also allowing directly appended 5+ digit versionCodes
                # like busuu's 32-41-01667052-release slug.
                ver_pattern = re.escape(current_ver_dash)
                if not re.search(rf'(?:^|[/-]){ver_pattern}(?:[/-]|\d{{5,}}(?=[/-]|$)|$)', href):
                    continue

                tier = 1
                if build_number and i == len(version_parts):
                    if build_format == 'build_suffix':
                        if not re.search(rf'{ver_pattern}-build-0*{int(build_number)}(?:[/-]|$)', href):
                            continue
                        tier = 0
                    elif (
                        f"{current_ver_dash}{build_number}" in href
                        or f"{current_ver_dash}-{build_number}" in href
                        or f"({build_number})" in link.get_text()
                    ):
                        tier = 0

                app_priority = 0 if exact_app_match else 1
                priority = 0 if href.rstrip('/').endswith('-release') else 1
                candidates.append((tier, app_priority, priority, len(link['href']), link['href']))

            if candidates:
                candidates.sort()
                chosen = candidates[0][4]
                full_url = base_url + chosen
                logging.info(f"✓ Found release page on main listing for {current_ver}: {full_url}")
                return full_url

    return None

def find_release_page_from_main(
    version: str,
    config: dict,
    build_number: str = None,
    build_format: str = None,
    allow_prerelease: bool = False,
) -> str | None:
    """Scrape the main app listing page on APKMirror to find the correct release page URL
    for a specific version. This avoids URL construction from config fields, which may not
    match APKMirror's actual URL slugs (e.g., 'duolingo' vs 'duolingo-language-lessons').
    
    Strategy:
    1. Try the configured main page (org/name from config)
    2. If that 404s, use APKMirror search to discover the correct main page URL
    3. Search APKMirror releases directly (finds older releases off page 1)
    
    Returns the full release page URL if found, or None if scraping fails."""
    try:
        # Step 1: Try configured main page first (works for most apps)
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        main_status, main_content = _get_main_page(main_url)
        search_cfg = config
        strict_search = False

        if main_status == 200:
            strict_search = True
            soup = BeautifulSoup(main_content, "html.parser")
            result = _scrape_release_url_from_soup(
                soup, version, config, build_number, build_format, allow_prerelease
            )
            if result:
                return result
            logging.debug(f"Main page accessible but no version match: {main_url}")
        else:
            logging.info(f"Configured main page returned {main_status}: {main_url}")
        
        # Step 2: Only discover alternate main URL when configured main_url failed
        if main_status != 200:
            discovered_url = discover_app_main_url(config)
            if discovered_url and discovered_url != main_url:
                logging.info(f"Trying discovered main page: {discovered_url}")
                d_resp = _cf_get(discovered_url)
                if d_resp.status_code == 200:
                    strict_search = True
                    disc_parts = discovered_url.rstrip("/").split("/")
                    if len(disc_parts) >= 2:
                        search_cfg = {**config, "name": disc_parts[-1]}
                    d_soup = BeautifulSoup(d_resp.content, "html.parser")
                    result = _scrape_release_url_from_soup(
                        d_soup, version, search_cfg, build_number, build_format, allow_prerelease
                    )
                    if result:
                        return result

        # Step 3: Release-level search fallback for older versions off page 1
        ver_query = f"{version} build {build_number}" if (build_number and build_format == "build_suffix") else version
        queries = []
        if config.get("package"):
            queries.append(f"{config['package']} {ver_query}")
        if config.get("name"):
            queries.append(f"{config['name'].replace('-', ' ')} {ver_query}")
        for q in dict.fromkeys(queries):
            if _blocked_by_cloudflare:
                break
            search_url = f"{base_url}/?post_type=app_release&searchtype=apk&s={quote(q)}"
            logging.info(f"Searching APKMirror releases: {search_url}")
            s_resp = _cf_get(search_url)
            if s_resp.status_code == 200:
                s_soup = BeautifulSoup(s_resp.content, "html.parser")
                result = _scrape_release_url_from_soup(
                    s_soup,
                    version,
                    search_cfg,
                    build_number,
                    build_format,
                    allow_prerelease,
                    strict_app=strict_search,
                )
                if result:
                    return result
        
        logging.debug(f"Could not find release page URL from main listing for version {version}")
        return None
        
    except ApkMirrorBlocked:
        raise
    except Exception as e:
        logging.debug(f"Error scraping main page for release URL: {e}")
        return None

def get_download_link(version: str, app_name: str, config: dict, arch: str = None) -> str:
    global _blocked_by_cloudflare
    _blocked_by_cloudflare = False
    _main_page_cache.clear()
    if not version:
        logging.error(f"No version provided for {app_name}")
        return None
    # Strip variant suffix from CLI version string (e.g., "18.0.3.954559732-release-arm64-v8a" -> "18.0.3.954559732")
    # The variant info is used for filtering, not URL construction.
    original_version = version
    want_prerelease = bool(re.search(r'(?:^|[-_. ])(?:beta|alpha)(?:$|[-_. ])', original_version, re.IGNORECASE))
    version = extract_base_version(version)
    if version != original_version:
        logging.info(f"Stripped variant from version: {original_version} -> {version}")
        
    target_arch = arch if (arch and arch != "universal") else config.get('arch', 'universal')
    
    criteria = [config['type'], target_arch, config['dpi']]
    
    # --- UNIVERSAL URL FINDER WITH VALIDATION ---
    # Extract build number if present (e.g., "32.30.0(1575420)" -> version="32.30.0", build="1575420")
    build_number = None
    build_format = None
    
    # Check for parentheses format: "32.30.0(1575420)"
    build_match = re.search(r'\((\d+)\)$', version)
    if build_match:
        build_number = build_match.group(1)
        build_format = 'parentheses'
        version = version[:build_match.start()]
    else:
        # Check for build suffix format: "6.6 build 002"
        build_match = re.search(r'\s+build\s+(\d+)$', version, re.IGNORECASE)
        if build_match:
            build_number = build_match.group(1)
            build_format = 'build_suffix'
            version = version[:build_match.start()]
        else:
            # Try to fetch build number from APKMirror for this version
            build_number, build_format = get_build_number_for_version(version, config)
            if build_number:
                logging.info(f"Found build number {build_number} for version {version} (format: {build_format})")

    if _blocked_by_cloudflare:
        return None
    
    version_parts = version.split('.')
    found_soup = None
    correct_version_page = False
    
    # --- PRIMARY APPROACH: Scrape the main app page for the correct release URL ---
    # This is more reliable than constructing URLs from config fields, because
    # APKMirror's actual URL slugs often differ from config values
    # (e.g., 'duolingo' slug vs 'duolingo-language-lessons' actual release name)
    scraped_url = find_release_page_from_main(
        version, config, build_number, build_format, allow_prerelease=want_prerelease
    )
    def _has_version_token(text: str, ver: str) -> bool:
        if not text or not ver:
            return False
        for cand in (ver, ver.replace(".", "-")):
            if re.search(rf"(?<![\d.]){re.escape(cand)}(?![\d.])", text):
                return True
        return False

    if scraped_url:
        logging.info(f"Trying scraped release URL: {scraped_url}")
        try:
            response = _cf_get(scraped_url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                page_text = soup.get_text()
                if _has_version_token(page_text, version):
                    logging.info(f"✓ Scraped release page validated: {response.url}")
                    found_soup = soup
                    correct_version_page = True
                else:
                    logging.warning(f"Scraped URL returned page but version {version} not found in content")
        except ApkMirrorBlocked:
            return None
        except Exception as e:
            logging.warning(f"Error fetching scraped URL: {e}")

    # Once Cloudflare has challenged or rate-limited this runner, generated URL probes cannot
    # succeed. Stop here so one app does not emit misleading 404s for every
    # possible release slug and the configured fallback can run immediately.
    if _blocked_by_cloudflare:
        return None
    
    # --- FALLBACK: Construct URLs from config fields ---
    # Only used if scraping the main page didn't work
    if not correct_version_page:
        logging.info("Scraping didn't find the page, falling back to URL construction...")
        # Use release_prefix if available, otherwise use app name
        release_name = config.get('release_prefix', config['name'])
        app_slugs = _app_slug_candidates(config)
        
        url_depths = [len(version_parts)]
        if len(version_parts) >= 3 and version_parts[-1] == '0':
            url_depths.append(len(version_parts) - 1)

        # Probe exact version (and optional trailing '.0' trim only; never drop non-zero parts)
        for i in url_depths:
            current_ver_str = "-".join(version_parts[:i])
            current_ver_dot = ".".join(version_parts[:i])
            
            # If build number exists, append it to the last version part in URL
            if build_number and i == len(version_parts):
                if build_format == 'build_suffix':
                    # e.g., "6-6" + "build-006" -> "6-6-build-006"
                    current_ver_str = current_ver_str + "-build-" + build_number
                else:
                    # e.g., "32-30-0" + "1575420" -> "32-30-01575420"
                    parts = version_parts[:i]
                    parts[-1] = parts[-1] + build_number
                    current_ver_str = "-".join(parts)
            
            # Generate ALL possible URL patterns in priority order
            url_patterns = []
            
            # URL-encode the release_name to handle unicode characters like ․
            encoded_release_name = quote(release_name, safe='')
            org = config.get('org', '')
            encoded_org = quote(org, safe='')

            for app_slug in app_slugs:
                encoded_name = quote(app_slug, safe='')

                # Prefer the explicit release slug; it is more stable than a
                # display name and supports apps whose title changes over time.
                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_release_name}-{current_ver_str}-release/")

                if release_name != app_slug:
                    url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_name}-{current_ver_str}-release/")

                if org and org != release_name and org != app_slug:
                    url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_org}-{current_ver_str}-release/")

                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_release_name}-{current_ver_str}/")

                if release_name != app_slug:
                    url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_name}-{current_ver_str}/")

                if org and org != release_name and org != app_slug:
                    url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_org}-{current_ver_str}/")
            
            # Remove duplicate patterns
            url_patterns = list(dict.fromkeys(url_patterns))
            
            for url in url_patterns:
                if _blocked_by_cloudflare:
                    return None
                logging.info(f"Checking potential release URL: {url}")
                
                try:
                    response = _cf_get(url)
                    if response.status_code == 200:
                        soup = BeautifulSoup(response.content, "html.parser")
                        page_text = soup.get_text()
                        
                        # VALIDATION: Check if this page actually serves variants for our version
                        has_variants = soup.find('div', class_='variants-table') is not None
                        version_checks = [
                            version,
                            version.replace('.', '-'),
                            current_ver_str,
                            current_ver_dot,
                        ]
                        if build_number:
                            if build_format == 'build_suffix':
                                version_checks.append(f"{version} build {build_number}")
                                version_checks.append(f"{version.replace('.', '-')}-build-{build_number}")
                            else:
                                version_checks.append(f"{version}({build_number})")

                        title_tag = soup.find('title')
                        headings = soup.find_all(['h1', 'h2', 'h3'])
                        
                        is_correct_page = False
                        for check in dict.fromkeys(c for c in version_checks if c):
                            if _has_version_token(page_text, check) and has_variants:
                                is_correct_page = True
                                break
                        if not is_correct_page and has_variants:
                            for node in [*headings, *([title_tag] if title_tag else [])]:
                                node_text = node.get_text()
                                if any(_has_version_token(node_text, c) for c in version_checks if c):
                                    is_correct_page = True
                                    break
                        
                        if is_correct_page:
                            logging.info(f"✓ Correct version page found: {response.url}")
                            found_soup = soup
                            correct_version_page = True
                            break
                        else:
                            logging.warning(f"Page found but not a variant release for version {version}: {url}")
                            if has_variants and found_soup is None:
                                found_soup = soup
                                logging.warning("Saved as fallback page (may list multiple versions)")
                            continue
                            
                    elif response.status_code == 404:
                        logging.info(f"URL not found (404): {url}")
                        continue
                    else:
                        logging.warning(f"URL {url} returned status {response.status_code}")
                        continue
                        
                except ApkMirrorBlocked:
                    return None
                except Exception as e:
                    logging.warning(f"Error checking {url}: {str(e)[:50]}")
                    continue
            
            if correct_version_page:
                break
    
    # If we didn't find the exact version page but found a fallback
    if not correct_version_page and found_soup:
        logging.warning(f"Using fallback page for {app_name} {version} (may contain multiple versions)")
    
    if not found_soup:
        logging.error(f"Could not find any release page for {app_name} {version}")
        return None
    
    # --- VARIANT FINDER (scoped to variants-table first to avoid sidebar links) ---
    table = found_soup.find('div', class_='variants-table')
    rows = table.find_all('div', class_='table-row') if table else []
    if not rows:
        rows = [
            r for r in found_soup.find_all('div', class_='table-row')
            if any(
                ('-android-apk-download' in a['href'] or a['href'].rstrip('/').endswith('-download'))
                and not a['href'].endswith('#disqus_thread')
                for a in r.find_all('a', href=True)
            )
        ]
    download_page_url = None
    
    def _row_matches(row_text: str, type_override: str | None = None) -> bool:
        r = row_text.lower()
        if 'variant' in r and 'arch' in r and 'version' in r:
            return False  # Skip header row
            
        c_type = (type_override if type_override is not None else (config.get('type') or '')).lower()
        if c_type and c_type not in r:
            return False
        
        t_arch = (target_arch or 'universal').lower()
        if t_arch in ['universal', 'noarch']:
            if not any(a in r for a in ['universal', 'noarch', 'arm64-v8a', 'armeabi-v7a', 'arm64', 'arm']):
                return False
        elif t_arch not in r and 'universal' not in r and 'noarch' not in r:
            return False

        c_dpi = (config.get('dpi') or 'nodpi').lower()
        if c_dpi in ['nodpi', '120-640dpi', 'all', '']:
            # All DPIs acceptable for universal/nodpi/bundle
            pass
        elif c_dpi not in r:
            dpi_num = re.fullmatch(r'(\d+)dpi', c_dpi)
            if dpi_num:
                target_dpi = int(dpi_num.group(1))
                in_range = any(
                    int(lo) <= target_dpi <= int(hi)
                    for lo, hi in re.findall(r'(\d+)-(\d+)dpi', r)
                )
                if not in_range:
                    return False
            else:
                return False

        return True

    # Helper to find download link in a row
    def _extract_row_link(row) -> str | None:
        for a in row.find_all('a', href=True):
            href = a['href']
            if '-android-apk-download' in href or href.rstrip('/').endswith('-download'):
                if not href.endswith('#disqus_thread'):
                    return base_url + href
        link = row.find('a', class_='accent_color')
        if link and 'href' in link.attrs and not link['href'].endswith('#disqus_thread'):
            return base_url + link['href']
        return None

    def _arch_priority(row_text: str) -> int:
        r = row_text.lower()
        t_arch = (target_arch or 'universal').lower()
        if t_arch not in ('universal', 'noarch'):
            return 0 if t_arch in r else 1
        if 'universal' in r or 'noarch' in r:
            return 0
        if 'arm64-v8a' in r or 'arm64' in r:
            return 1
        return 2

    def _best_row_link(require_version: bool = True, type_override: str | None = None) -> str | None:
        candidates = []
        for idx, row in enumerate(rows):
            row_text = row.get_text()
            if 'variant' in row_text.lower() and 'arch' in row_text.lower():
                continue
            if require_version and not _has_version_token(row_text, version):
                continue
            if _row_matches(row_text, type_override=type_override):
                link = _extract_row_link(row)
                if link:
                    candidates.append((_arch_priority(row_text), idx, link))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            return candidates[0][2]
        return None

    # Try exact version match first (preferring universal / arm64 over armeabi-only)
    download_page_url = _best_row_link(require_version=True)

    # Fall back from APK to BUNDLE on the exact release page when an app
    # migrated from standalone APKs to split APKM bundles (future-proofing).
    if not download_page_url and correct_version_page and (config.get('type') or '').lower() == 'apk':
        download_page_url = _best_row_link(require_version=True, type_override='bundle')
        if download_page_url:
            logging.info(f"APKMirror: using BUNDLE variant for {app_name} {version}")

    # On a confirmed exact release page, row text may omit the version string
    # if the release title carries it; never loosen version matching on multi-version fallbacks.
    if not download_page_url and correct_version_page:
        download_page_url = _best_row_link(require_version=False)
    
    if not download_page_url:
        logging.error(f"No variant found for {app_name} {version} with criteria {criteria}")
        # Debug: log what rows we found
        logging.debug(f"Found {len(rows)} rows total")
        for idx, row in enumerate(rows[:5]):  # First 5 rows
            logging.debug(f"Row {idx}: {row.get_text()[:100]}...")
        return None
    
    # --- STANDARD DOWNLOAD FLOW ---
    try:
        response = _cf_get(download_page_url)
        response.raise_for_status()
        content_size = len(response.content)
        logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> Variant Page")
        soup = BeautifulSoup(response.content, "html.parser")

        sub_url = soup.find('a', class_='downloadButton')
        if sub_url:
            final_download_page_url = base_url + sub_url['href']
            response = _cf_get(final_download_page_url)
            response.raise_for_status()
            content_size = len(response.content)
            logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> Download Page")
            soup = BeautifulSoup(response.content, "html.parser")

            button = soup.find('a', id='download-link')
            if button:
                return base_url + button['href']
    except Exception as e:
        logging.error(f"Error in download flow: {e}")

    return None

def get_architecture_criteria(arch: str) -> dict:
    """Map architecture names to APKMirror criteria"""
    arch_mapping = {
        "arm64-v8a": "arm64-v8a",
        "armeabi-v7a": "armeabi-v7a", 
        "universal": "universal"
    }
    return arch_mapping.get(arch, "universal")
    
def get_latest_version(app_name: str, config: dict) -> str:
    global _blocked_by_cloudflare
    _blocked_by_cloudflare = False
    # First try: get from main app page (e.g. /apk/org/name/)
    try:
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        response = _cf_get(main_url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            for h5 in soup.find_all("h5", class_="appRowTitle"):
                row_text = h5.get_text(strip=True)
                if any(skip in row_text.lower() for skip in ["wear os", "daydream", "automotive", "android tv", "beta", "alpha"]):
                    continue
                match = re.search(r'(\d+(\.\d+)+)', row_text)
                if match:
                    return match.group(1)
    except Exception:
        pass
    
    # Original method (keep exactly as you had it)
    url = f"{base_url}/uploads/?appcategory={config['name']}"
    
    response = _cf_get(url)
    response.raise_for_status()
    content_size = len(response.content)
    logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> \"-\" [1]")
    soup = BeautifulSoup(response.content, "html.parser")

    app_rows = soup.find_all("div", class_="appRow")
    version_pattern = re.compile(r'\d+(\.\d+)*(-[a-zA-Z0-9]+(\.\d+)*)*')

    for row in app_rows:
        title_h5 = row.find("h5", class_="appRowTitle")
        if not title_h5 or not title_h5.a:
            continue
        version_text = title_h5.a.get_text(strip=True) or ""
        if "alpha" not in version_text.lower() and "beta" not in version_text.lower():
            match = version_pattern.search(version_text)
            if match:
                version = match.group()
                version_parts = version.split('.')
                base_version_parts = []
                for part in version_parts:
                    if part.isdigit():
                        base_version_parts.append(part)
                    else:
                        break
                if base_version_parts:
                    base_version = '.'.join(base_version_parts)
                    
                    # Check for build number in parentheses like "32.30.0(1575420)"
                    build_match = re.search(r'\((\d+)\)', version_text)
                    if build_match:
                        build_number = build_match.group(1)
                        return f"{base_version}({build_number})"
                    
                    return base_version

    return None
