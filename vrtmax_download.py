#!/usr/bin/env python3
"""
VRT Max Show Downloader
Downloads all episodes from a VRT Max show page using N_m3u8DL-RE with Widevine decryption.

Usage:
    python vrtmax_download.py <show_url> --wvd <device.wvd> [options]

Example:
    python vrtmax_download.py https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd
    python vrtmax_download.py https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd --output ./downloads
    python vrtmax_download.py https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd --list-only
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MEDIA_API = "https://media-services-public.vrt.be/vualto-video-aggregator-web/rest/external/v2"
TOKEN_API = "https://token.vrt.be/vrtvideo"
GRAPHQL_API = "https://www.vrt.be/vrtmax/api/"

# Widevine system ID used to find PSSH in manifests
WIDEVINE_SYSTEM_ID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
            "Origin": "https://www.vrt.be",
            "Referer": "https://www.vrt.be/",
        }
    )
    return session


def get_page_data(url: str, session: requests.Session) -> dict:
    """Fetch a VRT Max page and extract the __NEXT_DATA__ JSON blob."""
    resp = session.get(url, headers={"Accept": "text/html,application/xhtml+xml,*/*"})
    resp.raise_for_status()

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not match:
        raise ValueError(
            "Could not find __NEXT_DATA__ on the page. "
            "The URL might be invalid or VRT Max changed their page structure."
        )
    return json.loads(match.group(1))


def find_episodes_in_data(data: dict, debug: bool = False) -> list[dict]:
    """
    Recursively search the __NEXT_DATA__ structure for episode entries.
    Returns a deduplicated list of episode dicts.
    """
    episodes = []

    def search(obj, depth=0):
        if depth > 15:
            return
        if isinstance(obj, list):
            for item in obj:
                search(item, depth + 1)
        elif isinstance(obj, dict):
            # An episode node typically has a videoId / publicationId
            has_video_id = "videoId" in obj or "publicationId" in obj
            has_title = "title" in obj or "episodeTitle" in obj or "name" in obj
            if has_video_id and has_title:
                video_id = obj.get("videoId") or obj.get("publicationId", "")
                title = (
                    obj.get("episodeTitle")
                    or obj.get("title")
                    or obj.get("name")
                    or "Unknown"
                )
                episode = {
                    "videoId": video_id,
                    "title": title,
                    "seasonNumber": obj.get("seasonNumber") or obj.get("season"),
                    "episodeNumber": obj.get("episodeNumber") or obj.get("episode"),
                    "seasonTitle": obj.get("seasonTitle", ""),
                }
                if episode["videoId"]:
                    episodes.append(episode)
            # Keep searching children even if this node looked like an episode
            for value in obj.values():
                search(value, depth + 1)

    search(data)

    # Deduplicate by videoId while preserving order
    seen: set[str] = set()
    unique: list[dict] = []
    for ep in episodes:
        if ep["videoId"] not in seen:
            seen.add(ep["videoId"])
            unique.append(ep)

    if debug and not unique:
        page_props = data.get("props", {}).get("pageProps", {})
        print(f"  DEBUG: pageProps top-level keys: {list(page_props.keys())}")

    return unique


def get_episodes_via_graphql(show_url: str, session: requests.Session, debug: bool = False) -> list[dict]:
    """
    Fallback: fetch episode list directly from VRT Max GraphQL API.
    The programUrl is derived from the show page URL (e.g. /vrtmax/a-z/juliet/).
    """
    # Extract program path from URL, e.g. /vrtmax/a-z/juliet/
    path_match = re.search(r"((?:/vrtmax)?/a-z/[^/?#]+/?)", show_url)
    if not path_match:
        return []
    program_url = path_match.group(1)
    if not program_url.startswith("/vrtmax"):
        program_url = "/vrtmax" + program_url

    query = """
    query ProgramEpisodes($programUrl: String!) {
      program(programUrl: $programUrl) {
        title
        episodes(first: 500) {
          edges {
            node {
              title
              videoId
              publicationId
              episodeNumber
              seasonNumber
              seasonTitle
            }
          }
        }
      }
    }
    """
    try:
        resp = session.post(
            GRAPHQL_API,
            json={"query": query, "variables": {"programUrl": program_url}},
            headers={"Content-Type": "application/json"},
        )
        if not resp.ok:
            if debug:
                print(f"  DEBUG: GraphQL returned {resp.status_code}")
            return []
        gql_data = resp.json()
        edges = (
            gql_data.get("data", {})
            .get("program", {})
            .get("episodes", {})
            .get("edges", [])
        )
        episodes = []
        for edge in edges:
            node = edge.get("node", {})
            video_id = node.get("videoId") or node.get("publicationId", "")
            if video_id:
                episodes.append(
                    {
                        "videoId": video_id,
                        "title": node.get("title", "Unknown"),
                        "seasonNumber": node.get("seasonNumber"),
                        "episodeNumber": node.get("episodeNumber"),
                        "seasonTitle": node.get("seasonTitle", ""),
                    }
                )
        return episodes
    except Exception as e:
        if debug:
            print(f"  DEBUG: GraphQL error: {e}")
        return []


def get_player_token(session: requests.Session) -> Optional[str]:
    """Obtain a VRT player token (needed for stream URL resolution)."""
    try:
        resp = session.post(
            TOKEN_API,
            json={"identityToken": ""},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("vrtPlayerToken")
    except Exception:
        pass
    return None


def get_stream_info(
    video_id: str,
    session: requests.Session,
    token: Optional[str] = None,
    debug: bool = False,
) -> dict:
    """Get stream info (manifest URL + DRM details) for a given videoId."""
    params: dict[str, str] = {"client": "vrtvideo@PROD"}
    if token:
        params["vrtPlayerToken"] = token

    # The media API may accept the videoId directly or via a publication compound ID
    urls_to_try = [
        f"{MEDIA_API}/videos/{video_id}",
    ]
    # If it looks like a bare vid-xxx ID, also try the publication compound format
    if video_id.startswith("vid-") and "$" not in video_id:
        urls_to_try.append(f"{MEDIA_API}/videos/pbs-pub-{video_id[4:]}${video_id}")

    for url in urls_to_try:
        try:
            resp = session.get(url, params=params, timeout=15)
            if resp.ok:
                return resp.json()
            if debug:
                print(f"  DEBUG: {url} -> {resp.status_code}")
        except Exception as e:
            if debug:
                print(f"  DEBUG: request error for {url}: {e}")

    raise RuntimeError(
        f"Could not retrieve stream info for videoId '{video_id}'. "
        "A player token might be required for this content (use --token)."
    )


def get_pssh_from_manifest(
    manifest_url: str, session: requests.Session, debug: bool = False
) -> Optional[str]:
    """Download a DASH manifest and extract the Widevine PSSH box (base64)."""
    resp = session.get(manifest_url, timeout=20)
    resp.raise_for_status()
    text = resp.text

    # Match a cenc:pssh inside a Widevine ContentProtection block
    # Pattern 1: explicit schemeIdUri with Widevine system ID
    pattern1 = (
        rf'schemeIdUri="urn:uuid:{WIDEVINE_SYSTEM_ID}"[^>]*>'
        r'(?:.*?)<cenc:pssh[^>]*>(.*?)</cenc:pssh>'
    )
    m = re.search(pattern1, text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()

    # Pattern 2: any cenc:pssh element (first one is usually Widevine on VRT Max)
    m = re.search(r"<cenc:pssh[^>]*>(.*?)</cenc:pssh>", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()

    if debug:
        print("  DEBUG: manifest snippet (first 2000 chars):")
        print(text[:2000])

    return None


def get_widevine_keys(
    pssh_b64: str,
    license_url: str,
    session: requests.Session,
    device: Device,
    license_headers: Optional[dict] = None,
) -> list[str]:
    """Use pywidevine to get content decryption keys from a license server."""
    cdm = Cdm.from_device(device)
    cdm_session = cdm.open()
    try:
        challenge = cdm.get_license_challenge(cdm_session, PSSH(pssh_b64))

        headers = {"Content-Type": "application/octet-stream"}
        if license_headers:
            headers.update(license_headers)

        resp = session.post(license_url, data=challenge, headers=headers, timeout=15)
        resp.raise_for_status()

        cdm.parse_license(cdm_session, resp.content)
        return [
            f"{key.kid.hex}:{key.key.hex()}"
            for key in cdm.get_keys(cdm_session)
            if key.type == "CONTENT"
        ]
    finally:
        cdm.close(cdm_session)


def build_episode_title(episode: dict) -> str:
    """Build a clean episode title string including season/episode numbers."""
    title = episode["title"]
    s = episode.get("seasonNumber")
    e = episode.get("episodeNumber")
    if s is not None and e is not None:
        try:
            return f"S{int(s):02d}E{int(e):02d} - {title}"
        except (ValueError, TypeError):
            pass
    if episode.get("seasonTitle"):
        return f"{episode['seasonTitle']} - {title}"
    return title


def safe_filename(name: str) -> str:
    """Remove characters that are illegal in filenames."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def download_episode(
    title: str,
    manifest_url: str,
    manifest_headers: dict,
    keys: list[str],
    output_dir: Path,
    extra_args: list[str],
) -> bool:
    """Invoke N_m3u8DL-RE to download and decrypt an episode."""
    cmd = ["N_m3u8DL-RE", manifest_url]

    for key, value in manifest_headers.items():
        cmd.extend(["-H", f'{key}: {value.replace(chr(34), chr(39))}'])

    for key in keys:
        cmd.extend(["--key", key])

    cmd.extend(["--save-name", safe_filename(title)])
    cmd.extend(["--save-dir", str(output_dir)])
    cmd.extend(extra_args)

    print(f"  Running: N_m3u8DL-RE \"{manifest_url[:70]}...\" [+{len(keys)} key(s)]")
    result = subprocess.run(cmd)
    return result.returncode == 0


def extract_dash_url(stream_info: dict) -> Optional[str]:
    """Pull the DASH manifest URL out of a stream_info response dict."""
    # Direct keys
    for key in ("dash", "dashUrl", "url"):
        if stream_info.get(key, "").endswith((".mpd", ".isml/.mpd", "manifest")):
            return stream_info[key]
        if stream_info.get(key):
            val = stream_info[key]
            if "mpd" in val or "dash" in val.lower():
                return val

    # targetUrls / urls arrays
    for array_key in ("targetUrls", "urls"):
        for entry in stream_info.get(array_key, []):
            if entry.get("type", "").upper() in ("MPEG_DASH", "DASH", "MPD"):
                return entry.get("url")
            if entry.get("url", "").endswith(".mpd"):
                return entry["url"]

    return None


def extract_license_info(stream_info: dict) -> tuple[Optional[str], dict]:
    """Return (license_url, extra_license_headers) from stream_info."""
    drm = stream_info.get("drm", {})

    # Widevine entry
    wv = drm.get("com.widevine.alpha", {}) if drm else {}
    license_url = wv.get("licenseUrl") or wv.get("license_url") or wv.get("url")
    extra_headers: dict = {}
    token = wv.get("token") or wv.get("customData") or wv.get("licenseToken")
    if token:
        extra_headers["X-AxDRM-Message"] = token

    # Fallback: some APIs put the license URL at the top level
    if not license_url:
        license_url = stream_info.get("licenseUrl") or stream_info.get("license_url")

    return license_url, extra_headers


def extract_manifest_headers(stream_info: dict) -> dict:
    """Extract HTTP headers that should accompany manifest requests."""
    if "headers" in stream_info and isinstance(stream_info["headers"], dict):
        return stream_info["headers"]
    for entry in stream_info.get("targetUrls", []):
        if isinstance(entry.get("headers"), dict):
            return entry["headers"]
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all episodes from a VRT Max show page",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd\n"
            "  %(prog)s https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd --output ./downloads\n"
            "  %(prog)s https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd --list-only\n"
            "  %(prog)s https://www.vrt.be/vrtmax/a-z/juliet/ --wvd device.wvd "
            "-- --select-video best --select-audio best\n"
        ),
    )
    parser.add_argument(
        "url",
        help="VRT Max show page URL (e.g. https://www.vrt.be/vrtmax/a-z/juliet/)",
    )
    parser.add_argument(
        "--wvd",
        required=True,
        help="Path to Widevine Device file (.wvd)",
    )
    parser.add_argument(
        "--output",
        default=".",
        help="Output directory (default: current directory)",
    )
    parser.add_argument(
        "--token",
        help="VRT player token (skip automatic token fetch; use for premium content)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List episodes without downloading",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print extra debug information",
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to N_m3u8DL-RE (after --)",
    )
    args = parser.parse_args()

    # Strip leading '--' separator if present
    extra_args = [a for a in args.extra if a != "--"]

    wvd_path = Path(args.wvd)
    if not wvd_path.exists():
        print(f"Error: WVD file not found: {wvd_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = create_session()

    # ------------------------------------------------------------------
    # Load Widevine device
    # ------------------------------------------------------------------
    print(f"Loading Widevine device: {wvd_path}")
    device = Device.load(wvd_path)

    # ------------------------------------------------------------------
    # Obtain player token
    # ------------------------------------------------------------------
    token = args.token
    if not token:
        print("Fetching player token...")
        token = get_player_token(session)
        if token:
            print("  OK")
        else:
            print("  Warning: could not get player token (premium content may fail)")

    # ------------------------------------------------------------------
    # Find episodes
    # ------------------------------------------------------------------
    print(f"\nFetching show page: {args.url}")
    episodes: list[dict] = []

    try:
        page_data = get_page_data(args.url, session)
        episodes = find_episodes_in_data(page_data, debug=args.debug)
    except Exception as e:
        print(f"  Warning: could not parse page data: {e}")

    if not episodes:
        print("  Falling back to GraphQL API...")
        episodes = get_episodes_via_graphql(args.url, session, debug=args.debug)

    if not episodes:
        print("\nNo episodes found.")
        print(
            "Tips:\n"
            "  • Make sure the URL points to a valid VRT Max show (e.g. /vrtmax/a-z/juliet/)\n"
            "  • Use --debug to inspect the page data structure\n"
            "  • VRT Max may have changed their API; check their network requests in DevTools"
        )
        sys.exit(1)

    print(f"Found {len(episodes)} episode(s)\n")

    # ------------------------------------------------------------------
    # List-only mode
    # ------------------------------------------------------------------
    if args.list_only:
        for i, ep in enumerate(episodes, 1):
            print(f"  {i:3d}. {build_episode_title(ep)}  [{ep['videoId']}]")
        return

    # ------------------------------------------------------------------
    # Download loop
    # ------------------------------------------------------------------
    success_count = 0
    fail_count = 0

    for i, episode in enumerate(episodes, 1):
        title = build_episode_title(episode)
        video_id = episode["videoId"]

        print(f"[{i}/{len(episodes)}] {title}")
        print(f"  videoId: {video_id}")

        try:
            # 1. Get stream info
            print("  Fetching stream info...")
            stream_info = get_stream_info(video_id, session, token, debug=args.debug)

            if args.debug:
                print(f"  DEBUG stream_info keys: {list(stream_info.keys())}")

            # 2. Extract DASH manifest URL
            dash_url = extract_dash_url(stream_info)
            if not dash_url:
                print("  Error: could not find DASH manifest URL in stream info")
                if args.debug:
                    print("  DEBUG stream_info:", json.dumps(stream_info, indent=2)[:800])
                fail_count += 1
                print()
                continue

            print(f"  Manifest: {dash_url[:80]}{'...' if len(dash_url) > 80 else ''}")
            manifest_headers = extract_manifest_headers(stream_info)
            license_url, license_headers = extract_license_info(stream_info)

            # 3. Extract PSSH from manifest
            print("  Parsing PSSH from manifest...")
            pssh = get_pssh_from_manifest(dash_url, session, debug=args.debug)

            # 4. Get Widevine keys
            keys: list[str] = []
            if pssh and license_url:
                print(f"  Getting keys from: {license_url[:60]}{'...' if len(license_url) > 60 else ''}")
                keys = get_widevine_keys(pssh, license_url, session, device, license_headers)
                print(f"  Got {len(keys)} content key(s)")
                if args.debug:
                    for k in keys:
                        print(f"    {k}")
            elif not pssh:
                print("  Info: no PSSH found – content may be unencrypted")
            elif not license_url:
                print("  Warning: no license URL found – skipping key retrieval")

            # 5. Download
            ok = download_episode(
                title=title,
                manifest_url=dash_url,
                manifest_headers=manifest_headers,
                keys=keys,
                output_dir=output_dir,
                extra_args=extra_args,
            )
            if ok:
                success_count += 1
                print("  Done!\n")
            else:
                fail_count += 1
                print("  Download failed!\n")

        except Exception as exc:
            print(f"  Error: {exc}")
            if args.debug:
                import traceback

                traceback.print_exc()
            fail_count += 1
            print()

    print(f"Finished: {success_count} succeeded, {fail_count} failed")


if __name__ == "__main__":
    main()
