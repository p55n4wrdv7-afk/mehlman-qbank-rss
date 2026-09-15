# Mehlman Medical Complete Qbank RSS

Creates one RSS feed containing the **full numbered Free Video Qbank archive** from:

`https://mehlmanmedical.com/category/free-video-qbank/`

It is designed for RSS readers such as **NetNewsWire**.

## Setup — about 5 minutes

### 1. Create a GitHub repository

Create a new **public** repository, for example:

`mehlman-qbank-rss`

### 2. Upload this package

Upload **all files and folders** from this package to the root of the repository.

Important: the `.github` folder is hidden on macOS. If dragging files in Finder, press **Command + Shift + .** to show hidden files, or upload the ZIP contents another way.

Your repository should contain:

```text
.github/workflows/update-feed.yml
scripts/build_feed.py
data/posts.json
docs/feed.xml
requirements.txt
README.md
```

### 3. Run the initial import

On GitHub:

1. Open **Actions**.
2. Select **Update Mehlman Qbank RSS**.
3. Click **Run workflow**.
4. Let the workflow finish successfully.

The first run walks through the complete paginated archive. Later runs are incremental and stop after reaching already-known posts.

### 4. Enable GitHub Pages

Open:

**Settings → Pages**

Under **Build and deployment** choose:

- Source: **Deploy from a branch**
- Branch: **main**
- Folder: **/docs**

Save.

### 5. Add the feed to NetNewsWire

If your GitHub username is `YOURNAME` and the repository is called `mehlman-qbank-rss`, your feed URL is:

```text
https://YOURNAME.github.io/mehlman-qbank-rss/feed.xml
```

In NetNewsWire use **File → New Web Feed** and paste that URL.

## Automatic updates

GitHub Actions checks for new posts every **6 hours**. You do not need to keep your Mac running.

You can also update immediately from **Actions → Update Mehlman Qbank RSS → Run workflow**.

## What is included?

The scraper keeps numbered Qbank posts whose title contains a Q number, such as:

- `HY USMLE Q #1644 – Gastro`
- `HY USMLE Q #1643 – Pulmonary`

This deliberately avoids unrelated navigation/guidance content that may appear in the same archive.

## Notes

- The initial crawl is intentionally throttled between pages.
- Existing items are saved in `data/posts.json`, so they remain in the RSS feed even after they disappear from the site's first page.
- Each post URL is its permanent RSS GUID, preventing duplicates in NetNewsWire.
- If WordPress markup changes substantially, `scripts/build_feed.py` may need its selectors updated.
