"""Resumable, retrying HTTP download shared by the download scripts.

Data goes to <dest>.part and is renamed to <dest> only once the byte count matches the server's size, so a file with
its final name is always complete. If the connection drops, the next attempt asks for the remaining bytes (HTTP Range)
instead of starting over. Rerunning a script after a crash, restart or Ctrl+C continues where it stopped.
"""
import os, re, sys, time

CHUNK = 4 * 1024 * 1024


def _total_from(r, have):
    cr = r.headers.get("Content-Range")              # "bytes 100-999/1000"
    if cr and "/" in cr and not cr.endswith("/*"):
        return int(cr.rsplit("/", 1)[1])
    cl = r.headers.get("Content-Length")
    return int(cl) + (have if r.status_code == 206 else 0) if cl else None


def fetch(session, url, dest, max_stalls=10, timeout=(30, 120)):
    """Download url to dest with resume. Gives up after max_stalls attempts in a row that add no bytes."""
    if os.path.exists(dest):
        print("  have", os.path.basename(dest)); return dest
    part = dest + ".part"
    stalls, attempt = 0, 0
    while True:
        attempt += 1
        have = os.path.getsize(part) if os.path.exists(part) else 0
        start_have = have
        try:
            hdr = {"Range": "bytes=%d-" % have} if have else {}
            with session.get(url, headers=hdr, stream=True, timeout=timeout, allow_redirects=True) as r:
                if r.status_code == 416:                 # asked past the end: .part is already whole
                    os.replace(part, dest); print("  done", os.path.basename(dest)); return dest
                r.raise_for_status()
                if have and r.status_code != 206:        # server ignored Range: start over
                    print("  server does not support resume; restarting", os.path.basename(dest)); have = 0
                total = _total_from(r, have)
                if have == 0:
                    print("  get ", os.path.basename(dest), "(%.2f GB)" % (total / 1e9) if total else "")
                else:
                    print("  resume", os.path.basename(dest), "at %.2f / %.2f GB" % (have / 1e9, (total or 0) / 1e9))
                next_mark = have + (total or 1e18) * 0.05
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk); have += len(chunk)
                        if have >= next_mark and total:
                            print("    %3d%%  %.2f GB" % (100 * have / total, have / 1e9), flush=True)
                            next_mark = have + total * 0.05
            if total is None or have == total:
                os.replace(part, dest); print("  done", os.path.basename(dest)); return dest
            raise IOError("short read: %d of %d bytes" % (have, total))
        except KeyboardInterrupt:
            print("\n  stopped; rerun the same command to resume", os.path.basename(dest)); raise
        except Exception as e:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            stalls = 0 if have > start_have else stalls + 1
            if stalls >= max_stalls:
                raise RuntimeError("giving up on %s after %d attempts with no progress: %s" % (url, stalls, e))
            wait = min(120, 5 * 2 ** min(stalls, 5))
            msg = re.sub(r"\s+", " ", str(e))[:160]
            print("  attempt %d failed (%s: %s); %.2f GB kept; retry in %ds"
                  % (attempt, type(e).__name__, msg, have / 1e9, wait), file=sys.stderr, flush=True)
            time.sleep(wait)
