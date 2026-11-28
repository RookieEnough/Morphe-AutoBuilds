<div align="center">

# 🔧 Automated Android Build Pipeline

[![Daily Build](https://img.shields.io/github/actions/workflow/status/RookieEnough/Morphe-AutoBuilds/patch.yml?label=Daily%20Build&style=for-the-badge&color=2ea44f)](https://github.com/RookieEnough/Morphe-AutoBuilds/actions/workflows/patch.yml)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/RookieEnough/Revanced-AutoBuilds?style=for-the-badge&color=orange)](LICENSE)


<p align="center">
  <a href="https://ko-fi.com/rookie_z" target="_blank"><img src="https://storage.ko-fi.com/cdn/kofi6.png?v=6" height="30" style="height:30px; border-radius:8px; display:inline-block;" alt="Donate via Ko-fi" /></a>
  &nbsp;&nbsp;
  <a href="https://buymeachai.ezee.li/RookieZ" target="_blank"><img src="https://raw.githubusercontent.com/TakiShiwa/donate-with-upi/ffbb38749891aeb62e758a3692698e346e3df2da/Button/SVG/UPI-light-blue-01.svg" height="30" style="height:30px; border-radius:8px; display:inline-block;" alt="Donate via UPI" /></a>
  <br />
  <a href="https://paypal.me/RookieEnough" target="_blank"><img src="https://raw.githubusercontent.com/stefan-niedermann/paypal-donate-button/master/paypal-donate-button.png" height="50" style="height:50px; border-radius:8px; display:inline-block; margin-top:8px;" alt="Donate via PayPal" /></a>
</p>



<p align="center">
  <strong>Config-driven CI builds for Android</strong><br>
  Multi-source • Multi-architecture • GitHub Actions Powered
</p>

<p align="center">
A sophisticated, automated pipeline that assembles installable Android applications for <strong>non-rooted devices</strong>. The system fetches build tooling, downloads base APKs from multiple sources, applies community build configurations, and produces architecture-specific outputs.
</p>

[![Report Bug](https://img.shields.io/badge/Report%20Bug-0A0A0A?style=flat&logo=github&logoColor=white)](https://github.com/RookieEnough/Morphe-AutoBuilds/issues)
[![Request Feature](https://img.shields.io/badge/Request%20Feature-0A0A0A?style=flat&logo=github&logoColor=white)](https://github.com/RookieEnough/Morphe-AutoBuilds/issues)


</div>

---

## ⚡ At a Glance

> **Note:** This repository hosts **no downloadable files**. Builds are produced automatically every day at 06:00 UTC for personal use. See the [Disclaimer](#️-disclaimer--legal) below.

### 📱 Supported Apps & Architectures

The full list of supported apps lives in [`patch-config.json`](patch-config.json) (95 apps and growing). Each app can target `arm64-v8a`, `armeabi-v7a`, and/or `universal` builds, configured in [`arch-config.json`](arch-config.json).

*( Legend: ✅ = Available / ❌ = Not configured )*

---

## ✨ Key Features

This repository utilizes a robust Python-based pipeline to ensure high reliability and optimization.

* **Fully Automated:** GitHub Actions workflow executes daily at 06:00 UTC, requiring zero manual intervention.
* **Architecture Optimization:** Builds specific `arm64-v8a`, `armeabi-v7a`, and `universal` APKs to reduce file size and improve performance on target devices.
* **Multi-Source Strategy:** Intelligent fetching from APKMirror, APKPure, and Uptodown ensures high success rates even if one source is down. Uptodown downloads resolve through its mobile API, which is not behind the Turnstile challenge that now guards the website download flow.
* **Granular Build Control:** Simple text-based configuration allows for precise inclusion or exclusion of specific build options.
* **Smart Failover:** The system automatically switches download sources if a fetch attempt fails.
* **Auto-Signing:** All APKs are signed with a consistent public keystore, making them ready to install immediately.
* **Clean Release Cycle:** Previous releases are replaced rather than archived, preventing clutter and making it easy for external managers (like Orion) to track updates.

---

## 🛠️ Repository Structure

```text
Morphe-AutoBuilds/
├── .github/workflows/      # GitHub Actions automation
│   ├── patch.yml           # Daily automated builds (06:00 UTC)
│   └── manual-patch.yml    # Manual trigger workflow
├── apps/                   # APK source configurations
│   ├── apkmirror/          # APKMirror definitions
│   ├── apkpure/            # APKPure definitions
│   └── uptodown/           # UptoDown definitions
├── patches/                # Build option rules
├── sources/                # Morphe tool source definitions
├── src/                    # Core Python build logic
├── arch-config.json        # Architecture build matrix
├── patch-config.json       # App build configuration
└── requirements.txt        # Project dependencies

```

---

## ⚙️ Configuration Guide

This builder is highly configurable. You can adjust the following files to customize the build output.

### 1. App Selection (`patch-config.json`)

Define which applications the pipeline should attempt to build.

```json
{
  "patch_list": [
    { "app_name": "youtube", "source": "morphe" },
    { "app_name": "youtube-music", "source": "morphe" },
    { "app_name": "X", "source": "crimera" }
  ]
}

```

### 2. Architecture Matrix (`arch-config.json`)

Specify which CPU architectures to target for each application.

```json
[
  {
    "app_name": "youtube",
    "source": "morphe",
    "arches": ["arm64-v8a", "armeabi-v7a", "universal"]
  },
  {
    "app_name": "youtube-music",
    "source": "morphe",
    "arches": ["arm64-v8a", "armeabi-v7a"]
  }
]

```

### 3. Source Definitions

Located in the `apps/` directory. Example for `apps/apkmirror/youtube.json`:

```json
{
  "org": "google-inc",
  "name": "youtube",
  "type": "APK",
  "arch": "universal",
  "dpi": "nodpi",
  "package": "com.google.android.youtube",
  "version": ""
}

```

### Uptodown downloads (mobile API)

Uptodown moved its website download flow behind Cloudflare Turnstile in
mid/late 2026. Direct `curl`/`wget` requests, and therefore shared
GitHub-hosted runner IPs, are treated as high risk and cannot pass that
challenge. The pipeline therefore resolves Uptodown downloads through the same
anonymous API the official Android app uses, as
[Obtainium](https://github.com/ImranR98/Obtainium) does:

1. `POST https://www.uptodown.app/eapi/auth/token` with a signed anonymous
   identifier (`HMAC-SHA256` over a unix timestamp) returns a short-lived
   bearer token.
2. `GET /eapi/apps/<app code>/file/<file id>/downloadUrl` resolves a catalog
   file id to a signed `dw.uptodown.com` URL that serves the APK/XAPK directly.

The catalog HTML is still used to discover the app code, versions, and file
ids; the HTML download-button flow and FlareSolverr remain only as fallbacks.
Implementation: [`src/uptodown_api.py`](src/uptodown_api.py), wired into
[`src/uptodown.py`](src/uptodown.py).

The anonymous client credentials can rotate, so they are overridable without a
code change (set them as repository variables or step `env`):

| Variable | Purpose | Default |
| --- | --- | --- |
| `UPTODOWN_API_CLIENT_VERSION` | Android client version sent as `Identificador-Version` | `739` |
| `UPTODOWN_API_HMAC_KEY` | Anonymous client signing key | built-in |
| `UPTODOWN_API_USER_AGENT` | Android `Dalvik` user agent | built-in |
| `UPTODOWN_API_HOST` | API host | `https://www.uptodown.app` |

Offline regression: `python tests/test_uptodown_api.py`.

### Play Store update protection

TikTok, Twitter and X builds set `android:versionCode` to `2147483647` before
alignment and signing, matching Morphe's **Disable Play Store updates** patch.
Package IDs, `versionName`, patch selection and the existing signing key stay
unchanged. This prevents ordinary higher-version Play Store update offers; it
is not a root-level detach and does not erase Play Store's cached listings.

Future protected builds can replace protected installs at the same version code.
Returning to a stock/lower-version-code build requires uninstalling first (with
potential data loss). Orion's current update detection compares `versionName`,
so the fixed code does not hide newer Orion versions. When first deploying this
change, force a full rebuild so the incremental release does not carry over old
APKs.

Requires Android SDK `zipalign` with 16 KB page-alignment support alongside
`apksigner`. Run the offline regression check with
`python tests/test_play_updates.py`.

### 4. Build Options

Located in `patches/`. Example for `patches/youtube-morphe.txt`. Use `+` to force include and `-` to exclude.

```text
# Essential options
+ microg-support
+ premium-heading
+ hide-infocard-suggestions

# Exclusions
- custom-branding
- amoled

```

### ABI selection policy

Every app is built along one preference order:

```text
universal -> arm64-v8a -> armeabi-v7a
```

* A **universal** APK installs on every device, so it is attempted first.
* If no source can produce it (for example when the only download for a version
  is a 32-bit split), the build is retried as **arm64-v8a**, and only if that
  also fails as **armeabi-v7a**. The pipeline never silently ships a lower-ABI
  build under the `universal` name.
* Explicit entries in [`arch-config.json`](arch-config.json) are still honoured:
  an app listed for `arm64-v8a` and `armeabi-v7a` produces both, and each target
  falls back down the same order if it cannot be built.

The order lives in [`src/downloader.py`](src/downloader.py) (`ARCH_PRIORITY`,
`fallback_arches`) and is applied by the build loop in
[`src/__main__.py`](src/__main__.py). Offline regression:
`python tests/test_arch_fallback.py`.

If a patch bundle reports **no compatible versions** for an app, the build now
stops immediately with an explicit error instead of downloading the store's
latest APK and refusing to sign an unpatched result — that usually means the
app's patch source dropped support (or was never updated) for that package.

---

## 🚀 Local Build Instructions

If you prefer to build the APKs on your own machine, follow these steps.

### Prerequisites

* Python 3.11 or higher
* Java Runtime Environment (JRE)
* `zip` utility
* `apksigner` (part of Android SDK Build-Tools)

### Installation & Execution

1. **Clone the repository:**
```bash
git clone https://github.com/RookieEnough/morphe-AutoBuilds.git
cd Morphe-AutoBuilds

```


2. **Install dependencies:**
```bash
pip install -r requirements.txt
pip install requests beautifulsoup4

```


3. **Run the build:**
You can build for a specific app and source.
```bash
export APP_NAME="youtube"
export SOURCE="morphe"
python -m src

```


4. **Target specific architecture (Optional):**
```bash
export APP_NAME="youtube"
export SOURCE="morphe"
export ARCH="arm64-v8a"  # Options: arm64-v8a, armeabi-v7a, universal
python -m src

```



---

## 🔄 GitHub Actions Workflows

### Daily Automated Build (`patch.yml`)

* **Schedule:** Runs daily at 06:00 UTC.
* **Function:** Iterates through all configured apps and architectures.
* **Output:** Refreshed build outputs for the configured apps.

### Manual Build (`manual-patch.yml`)

* **Trigger:** Manually via the GitHub Actions "Run workflow" button.
* **Capabilities:**
* Target specific apps.
* Target specific architectures.
* Force specific APK versions.
* Option to update the public release or just build artifacts.



---

## 🤝 Contributing

Contributions to improve the toolchain or add support for new apps are welcome.

1. **Fork** the repository.
2. **Create** a feature branch (`git checkout -b feature/new-app`).
3. **Test** your changes locally using the Python scripts.
4. **Commit** your changes (`git commit -m "Add support for new-app"`).
5. **Push** to the branch (`git push origin feature/new-app`).
6. **Open** a Pull Request.

---

## ⚠️ Disclaimer & Legal

> **Important:** This repository is a build-automation toolkit. It contains **no APK files, no binaries, and no copyrighted media** of any kind. Nothing here is a downloadable app.

* **No binaries hosted:** All build outputs are produced locally or in CI and are never committed to this repository.
* **Educational purpose:** The workflows and scripts are provided for educational and research purposes, to demonstrate automated Android build pipelines.
* **Affiliation:** This project is **not** affiliated with the Morphe Team or with any app publisher whose software it can process.
* **Your responsibility:** You are responsible for complying with the licenses and terms of service of the apps and build sources you choose to use. Do not redistribute builds in ways that violate those terms.
* **Use at your own risk:** Builds are generated automatically from third-party sources and may contain experimental features.
* **GmsCore:** Morphe's MicroG-RE is required for these non-root apps to function correctly.

---

<div align="center">

**If you found this project helpful, please consider giving it a ⭐ Star.**  
<br>
**Made with 💜 by RookieZ**

