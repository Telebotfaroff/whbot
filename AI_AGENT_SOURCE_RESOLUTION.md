# AI Agent Information — Source Fetching & Download URL Resolution

## Purpose

This document explains how WatchHentaiBot (WHBot) obtains data from the source website and how it resolves the **actual media/download URL** used by the downloader.

AI agents working on this repository should read this document before changing the provider, resolver, or downloader flow.

---

## 1. High-Level Data Flow

The current architecture is:

```text
User sends series URL
        |
        v
main.py
  resolve_series()
        |
        +--> WatchHentai.get_series()
        |       |
        |       +--> GET /series/<slug>/
        |       +--> extract title / thumbnail
        |
        +--> WatchHentai.series_episodes()
                |
                +--> GET /series/<slug>/
                +--> extract /videos/<episode> URLs
                        |
                        v
                Series preview shown to user
                        |
              User presses Download All
                        |
                        v
                download_series()
                        |
                        +--> WatchHentai.get_episode()
                                |
                                +--> GET episode /videos/<slug>/
                                |
                                +--> find data-primary-player-url
                                |
                                +--> GET player URL
                                |
                                +--> find whJwSources JSON
                                |
                                +--> decode source["file"]
                                |
                                v
                         Actual media URL(s)
                                |
                                v
                       download_and_send()
                                |
                                +--> WatchHentai.download()
                                |
                                +--> HTTP GET actual media URL
                                |       with Referer
                                |       and redirects enabled
                                |
                                v
                         Local video file
                                |
                                v
                         GoFile upload
```

The important distinction is:

- **Series URL** is only catalog/episode metadata.
- **Episode URL** is a page containing player metadata.
- **Player URL** contains the encoded media source list.
- **Decoded `source["file"]` URL** is the URL passed to the media downloader.
- The downloader then follows HTTP redirects and reports the final response URL.

---

## 2. Series Discovery

The series workflow starts in `main.py`:

- `resolve_series(series_url)`
- `provider.get_series(series_url)`
- `provider.series_episodes(series["series_url"])`

### `get_series()`

Implemented by:

```python
WatchHentai.get_series()
    -> _series_page()
```

`_series_page()` performs an HTTP GET using `_get()`.

The request uses:

- a browser-like User-Agent
- a Referer
- an HTML Accept header
- a connect/read timeout

The series page is parsed for:

- `og:title` → series name
- `og:image` → thumbnail
- episode links containing `/videos/`

The episode links are considered authoritative for the episode count when they are present.

### `series_episodes()`

This fetches the series page and calls:

```text
_episode_links(html)
```

That parser searches for links containing `/videos/`, converts them to absolute URLs, removes fragments, de-duplicates them, and returns objects like:

```json
{
  "provider": "watchhentai",
  "page_url": "https://watchhentai.net/videos/..."
}
```

No media URL is resolved at this stage.

---

## 3. Important Design Decision: Do Not Resolve Media During Preview

The series preview intentionally does **not** resolve actual media sources.

In `main.py`, `start_series_download()` only:

1. validates the series URL
2. gets series metadata
3. collects episode page URLs
4. displays the preview

Actual media resolution happens later inside `download_series()`, immediately before each episode is downloaded.

This is intentional.

An AI agent must not move source resolution into the initial series preview unless there is a specific reason to do so.

Reasons:

- avoids unnecessary requests for episodes the user never downloads
- avoids resolving stale player/source data too early
- keeps the preview lightweight
- resolves the source immediately before downloading

---

## 4. Episode Page Resolution

The critical resolver is:

```python
WatchHentai.get_episode(page_url, resolve_sources=True)
```

The flow is:

```text
Episode URL
   |
   +--> GET episode HTML
   |
   +--> extract title / thumbnail / synopsis
   |
   +--> find data-primary-player-url
   |
   +--> convert it to an absolute URL
   |
   +--> GET player HTML
   |
   +--> parse whJwSources
   |
   +--> decode every source["file"]
   |
   v
sources[]
```

### Step 1 — Fetch the episode page

`get_episode()` calls:

```python
html = self._get(page_url)
```

The page URL must contain:

```text
watchhentai.net/videos/
```

Otherwise `ProviderError` is raised.

---

## 5. Finding the Actual Player Page

The episode HTML is searched for:

```text
data-primary-player-url="..."
```

The relevant code is:

```python
m = re.search(
    r'data-primary-player-url=["\\\']([^"\\\']+)',
    html,
    re.I,
)
```

If this attribute is missing:

```text
ProviderError("Primary player URL not found")
```

is raised.

The extracted value is passed through:

```python
self._absolute(...)
```

so relative player URLs become absolute URLs.

Then the player page is fetched with the episode URL as its Referer:

```python
player_html = self._get(player, page_url)
```

This player page is the important second stage of the resolver.

---

## 6. Finding the Encoded Source List

The player HTML contains a JavaScript variable named:

```text
whJwSources
```

The resolver searches for:

```text
var whJwSources = [ ... ];
```

using:

```python
re.search(
    r"var\\s+whJwSources\\s*=\\s*(\\[[\\s\\S]*?\\]);",
    html,
)
```

The captured array is parsed with:

```python
json.loads(...)
```

Each source normally contains information such as:

```json
{
  "label": "1080p",
  "type": "video/mp4",
  "file": "ENCODED_VALUE"
}
```

The bot does **not** assume that the `file` value is already a usable URL.

---

## 7. How the Encoded `file` Value Becomes the Media URL

This is the most important part of the resolver.

The implementation is:

```python
def _decode(value):
    value = value.replace("-", "+").replace("_", "/")
    value += "=" * (-len(value) % 4)
    decoded = base64.b64decode(value)
    decoded = bytes(
        byte ^ ((13 + (i % 17)) & 255)
        for i, byte in enumerate(decoded)
    )[::-1]
    return base64.b64decode(decoded).decode("utf-8")
```

The transformation has four stages.

### Stage A — URL-safe Base64 normalization

The source value first replaces:

```text
-  -> +
_  -> /
```

Then Base64 padding is restored:

```python
value += "=" * (-len(value) % 4)
```

### Stage B — First Base64 decode

The normalized value is Base64-decoded:

```python
decoded = base64.b64decode(value)
```

This produces bytes.

### Stage C — XOR transformation and byte reversal

Every decoded byte is XORed with a position-dependent key:

```text
key(i) = (13 + (i mod 17)) & 255
```

Equivalent implementation:

```python
bytes(
    byte ^ ((13 + (i % 17)) & 255)
    for i, byte in enumerate(decoded)
)
```

The resulting byte sequence is then reversed:

```python
...[::-1]
```

### Stage D — Second Base64 decode

The transformed bytes are Base64-decoded again and interpreted as UTF-8:

```python
base64.b64decode(decoded).decode("utf-8")
```

The final UTF-8 string is the media URL stored in:

```text
source["url"]
```

This is the URL the downloader receives.

---

## 8. Source Objects Returned by the Provider

After decoding, `_sources()` converts every valid source into:

```json
{
  "label": "1080p",
  "type": "video/mp4",
  "url": "https://...actual-media-url..."
}
```

The provider returns all successfully decoded sources.

Failed individual source decodes are logged and skipped rather than crashing the entire source parser.

Example internal logging:

```text
[resolver] whJwSources entries: N
[resolver] source #1 label=1080p type=video/mp4 url=...
[resolver] source #2 label=720p type=video/mp4 url=...
[resolver] resolved sources: N
```

These `[resolver]` logs are useful when debugging source changes.

---

## 9. Quality Selection

For a normal single-episode download, `main.py` receives the resolved source list and uses:

```python
choose_source(ep, preferred_quality)
```

The preferred quality is selected when available.

If no preferred quality is supplied, the current priority is:

```text
2160p
1440p
1080p
720p
```

The selected object contains the decoded media URL:

```python
source["url"]
```

That value is passed to:

```python
provider.download(...)
```

---

## 10. Series Download Resolution

For a complete series, `download_series()` deliberately performs resolution per episode:

```python
ep = await asyncio.to_thread(
    provider.get_episode,
    item["page_url"],
    True,
)
```

This means every episode independently goes through:

```text
episode page
    -> primary player URL
    -> player page
    -> whJwSources
    -> decode file
    -> resolved sources
    -> choose quality
    -> download
```

If one quality fails, the current code tries the other available resolved sources for that episode.

---

## 11. How the Actual Media File Is Downloaded

The final media URL is passed to:

```python
WatchHentai.download(url, output, progress=..., referer=...)
```

The downloader:

1. sends an HTTP GET
2. uses the source website as the Referer
3. enables `stream=True`
4. enables `allow_redirects=True`
5. writes the response in 1 MB chunks
6. reports progress
7. validates the response before accepting it as a video

The downloader records:

```text
[downloader] URL: ...
[downloader] HTTP status: ...
[downloader] final URL: ...
[downloader] content-type: ...
[downloader] content-length: ...
```

### Important distinction

The decoded `source["url"]` is the **initial media URL**.

Because redirects are enabled, the HTTP server may return a different final URL. The downloader logs that as:

```text
[downloader] final URL: ...
```

The code does not need to manually resolve HTTP redirects first.

---

## 12. Media Validation

The downloader does not blindly save every HTTP response.

It checks the first response chunk.

It rejects responses that look like HTML, for example:

```text
<html
<!doctype
```

It also expects a recognized video/octet-stream content type or an MP4 `ftyp` signature.

This protects the bot from saving an error page as a video when a media URL has expired or the source server changes behavior.

---

## 13. Referer Handling

The provider's common request headers include:

```text
User-Agent
Referer
Accept
```

For player resolution:

```text
Referer = episode page URL
```

For media download:

```text
Referer = episode page URL (when supplied)
```

An AI agent changing the resolver should preserve this behavior unless the source website's requirements have been verified.

---

## 14. What an AI Agent Must Not Assume

Do not assume:

1. The series page contains the final media URL.
2. The episode page directly contains a usable MP4 URL.
3. `data-primary-player-url` is itself the video URL.
4. `whJwSources[].file` is plain text.
5. The first source is always the best quality.
6. The decoded URL will never redirect.
7. A successful HTTP response always contains video data.
8. A player page can be skipped.
9. Source URLs should be permanently cached as if they were stable assets.

The current code intentionally resolves media sources at download time.

---

## 15. Files Responsible for This Pipeline

### `providers/watchhentai.py`

Main provider/resolver implementation:

- `_get()` — HTTP page fetching
- `_absolute()` — absolute URL construction
- `_episode_links()` — episode URL extraction
- `_series_page()` — series metadata extraction
- `series_episodes()` — episode collection
- `get_episode()` — episode/player/source resolution
- `_sources()` — `whJwSources` parsing
- `_decode()` — encoded media URL decoding
- `download()` — actual media HTTP download

### `main.py`

Orchestrates when the provider functions are called:

- `resolve_series()`
- `start_series_download()`
- `download_series()`
- `download_and_send()`
- `choose_source()`
- callback handling

### `gofile_uploader.py`

Runs only after the video has been downloaded locally.

It uploads the completed local file to GoFile and returns the GoFile download page.

---

## 16. Safe Modification Rule for AI Agents

When modifying the provider, preserve this contract:

```text
get_episode(page_url, True)
        |
        v
{
    "page_url": "...",
    "player_url": "...",
    "sources": [
        {
            "label": "...",
            "type": "...",
            "url": "ACTUAL_MEDIA_URL"
        }
    ]
}
```

The rest of WHBot expects `sources[].url` to be directly usable by `provider.download()`.

If the source website changes its player structure or encoding algorithm, update the resolver in `providers/watchhentai.py` while keeping this returned data contract stable whenever possible.

---

## 17. Debugging Checklist

When downloads stop working, inspect the pipeline in this exact order:

### A. Series URL problem

Check:

```text
[series preview]
```

Confirm that episode page URLs are being collected.

### B. Episode page problem

Check:

```text
[resolver] get_episode: ...
```

Confirm the episode HTML loads successfully.

### C. Player URL problem

Check:

```text
[resolver] player URL: ...
```

If this is missing, `data-primary-player-url` probably changed.

### D. Source list problem

Check:

```text
[resolver] whJwSources entries: ...
```

If it says `not found`, inspect the player HTML structure.

### E. Encoding problem

Check:

```text
[resolver] source #... decode failed: ...
```

If decoding fails, the site's source encoding algorithm may have changed.

### F. Media server problem

Check:

```text
[downloader] HTTP status: ...
[downloader] final URL: ...
[downloader] content-type: ...
```

If the decoded URL exists but the response is HTML, expired, forbidden, or otherwise invalid, investigate the media server/request headers rather than the series parser.

---

## 18. Short Version for AI Agents

When an AI agent needs to understand the resolver quickly:

```text
1. Fetch series page.
2. Extract /videos/ episode page URLs.
3. Do NOT resolve media sources during preview.
4. When downloading an episode, fetch its /videos/ page.
5. Extract data-primary-player-url.
6. Fetch that player page with the episode URL as Referer.
7. Find var whJwSources = [...].
8. JSON-parse the source list.
9. For each source["file"]:
   - URL-safe Base64 normalization
   - restore padding
   - Base64 decode
   - position-dependent XOR using (13 + i % 17)
   - reverse bytes
   - Base64 decode again
   - UTF-8 decode
10. The resulting string is sources[].url.
11. Select the requested/best quality.
12. Pass sources[].url to WatchHentai.download().
13. Downloader follows HTTP redirects and validates that the response is actually video data.
14. Upload the resulting local file to GoFile.
```

This is the current source-resolution contract of WHBot.
