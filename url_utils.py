"""
url_utils.py — Shared URL utility functions.
"""

import re


def url_to_slug(url: str) -> str:
    """
    Extract a stable filename stem from a job URL.

    Priority:
      1. Numeric job ID from the URL path — e.g. seek.com.au/job/91995152 -> "91995152"
         Handles: /job/123, /jobs/view/123, /position/123 etc.
      2. Sanitised URL fallback (strips query string and fragment).

    Two URLs for the same job with different ?ref= or #sol= params
    resolve to the same filename and are correctly identified as duplicates.
    """
    m = re.search(
        r"/(?:jobs?|jobdetail|position|opening|vacancy)(?:/[^/]+)?/(\d{5,})",
        url, re.IGNORECASE
    )
    if m:
        return m.group(1)

    # Fallback — strip query + fragment, sanitise remaining chars
    slug = re.sub(r"https?://", "", url)
    slug = slug.split("?")[0].split("#")[0]
    slug = re.sub(r'[/:*?"<>|&=#\\]', "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:120]
