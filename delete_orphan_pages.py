#!/usr/bin/env python3
"""
Utility script: Delete orphan/empty pages from BookStack.

Orphan pages are defined here as pages whose HTML content is effectively empty
after stripping tags (for example, many auto-created "Introduction" pages
with no real content).

Usage examples:

    # Dry run: list all empty pages named "Introduction" (any book)
    python delete_orphan_pages.py --title "Introduction" --dryrun

    # Delete empty "Introduction" pages only within a specific book
    python delete_orphan_pages.py --title "Introduction" --book-id 6690

    # Delete ALL empty pages across all books (be careful!)
    python delete_orphan_pages.py --dryrun
    python delete_orphan_pages.py
"""

import argparse
import requests

from migrate import (
    fetch_bookstack_pages,
    delete_bookstack_page,
    adaptive_sleep,
    BOOKSTACK_BASE_URL,
    bookstack_headers,
)

try:
    # Optional: better HTML inspection when BeautifulSoup is available
    from bs4 import BeautifulSoup
    _bs_available = True
except ImportError:
    BeautifulSoup = None
    _bs_available = False


def fetch_page_details(page_id):
    """Fetch full page details including HTML content from BookStack API.
    
    Handles 429 rate limiting by using the shared adaptive_sleep/backoff logic.
    Always sleeps after fetching to avoid rate limits.
    Returns None if the page cannot be fetched after a retry, in which case the
    caller should treat the page as *non-empty* to avoid accidental deletion.
    """
    url = f'{BOOKSTACK_BASE_URL}/api/pages/{page_id}'
    try:
        response = requests.get(url, headers=bookstack_headers)
        if response.status_code == 429:
            # Respect rate limiting and retry once
            print(f'    Rate limited when fetching page {page_id}, backing off and retrying...')
            adaptive_sleep()
            response = requests.get(url, headers=bookstack_headers)
        response.raise_for_status()
        # Always sleep after fetching details to avoid rate limits
        adaptive_sleep()
        return response.json()
    except Exception as e:
        print(f'    Warning: Failed to fetch page {page_id} details: {e}')
        return None


def find_page_by_slug(slug, book_slug=None):
    """Try to find a page by its slug, optionally within a specific book.
    
    This searches through all fetched pages first, then tries to find it via API
    if not found. Returns the page object if found, None otherwise.
    """
    # First, try searching in already-fetched pages
    all_pages = fetch_bookstack_pages()
    for page in all_pages:
        if page.get("slug") == slug:
            if book_slug is None or page.get("book_slug") == book_slug:
                return page
    
    # If not found, the page might be a draft or in a special state
    # Try to find it by searching all books
    print(f'    Page with slug "{slug}" not found in standard list. It might be a draft or require special access.')
    return None


def is_page_effectively_empty(page, min_text_length: int = 10, fetch_details: bool = True) -> bool:
    """
    Determine if a BookStack page is "empty".

    Heuristics:
      - No HTML at all
      - HTML is just trivial placeholders like <p></p> or <p><br></p>
      - After stripping tags, there is no meaningful text (length <= min_text_length)
      - No meaningful content elements (headings, lists, images, code blocks, tables)
    
    Args:
        page: Page object from BookStack API (may not have 'html' field if from list endpoint)
        min_text_length: Minimum text length to consider non-empty
        fetch_details: If True and 'html' not present, fetch full page details
    """
    # Check if we have HTML or markdown in the page object
    html = page.get("html")
    markdown = page.get("markdown")
    
    # If we don't have both html and markdown fields, or if they're empty strings,
    # fetch full page details to ensure we check the actual content
    # (The list endpoint may not include content, or may have empty strings)
    needs_fetch = False
    if fetch_details:
        # Fetch if either field is missing (None) or if both are empty strings
        if html is None or markdown is None:
            needs_fetch = True
        elif (html or "").strip() == "" and (markdown or "").strip() == "":
            # Both are empty strings - fetch to be sure
            needs_fetch = True
    
    if needs_fetch:
        page_id = page.get("id")
        if page_id:
            full_page = fetch_page_details(page_id)
            if full_page:
                html = full_page.get("html")
                markdown = full_page.get("markdown")
                # Update the page dictionary so debug output can access the fetched values
                page["html"] = html
                page["markdown"] = markdown
            else:
                # If we could not fetch details (e.g. rate limited), play it safe
                # and treat the page as non-empty so we don't delete it by mistake.
                return False
    
    # Check markdown first if available (BookStack may store content as markdown)
    # Even if HTML exists, markdown might be the actual content
    if markdown:
        markdown = markdown.strip()
        if markdown:
            # Check for markdown structure indicators (headings, lists, code, images)
            if any(marker in markdown for marker in ['#', '*', '-', '`', '![']):
                return False  # Has markdown structure, not empty
            # Check text length
            text = ' '.join(markdown.split())
            if len(text) > min_text_length:
                return False
            # Very short markdown is considered empty
            return len(text) <= min_text_length
    # Note: if markdown is None or empty string, we continue to HTML check
    
    # Check HTML (only if markdown is empty/None)
    html = (html or "").strip()

    if not html:
        # Both markdown and html are empty/None - page is empty
        return True

    # Common empty placeholders we generate for BookStack
    trivial_html = {
        "<p></p>",
        "<p><br></p>",
        "<p><br/></p>",
        "<p>&nbsp;</p>",
        "<p> </p>",
    }
    html_lower = html.lower().strip()
    if html_lower in trivial_html:
        return True

    # If html is empty, we already returned True above, so we should never reach here
    # But just in case, double-check
    if not html:
        return True
    
    # If BeautifulSoup is available, use it for better analysis
    if _bs_available:
        try:
            soup = BeautifulSoup(html, "html.parser")
            
            # Check for meaningful content elements
            # If page has headings, lists, images, code blocks, or tables, it's not empty
            has_headings = bool(soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']))
            has_lists = bool(soup.find_all(['ul', 'ol', 'li']))
            # Check for both standard <img> tags and Confluence <ac:image> macros
            has_images = bool(soup.find_all('img') or soup.find_all('ac:image'))
            has_code = bool(soup.find_all(['pre', 'code']))
            has_tables = bool(soup.find_all('table'))
            
            if has_headings or has_lists or has_images or has_code or has_tables:
                return False  # Has meaningful structure, not empty
            
            # Get text content
            text = soup.get_text(separator=' ', strip=True)
        except Exception:
            # Fallback if BeautifulSoup fails
            text = html
    else:
        # Fallback: crude text approximation
        text = html.replace("<br>", " ").replace("</p>", " ").replace("<p>", " ")
        text = text.strip()

    # Remove extra whitespace and check length
    text = ' '.join(text.split())
    return len(text) <= min_text_length


def main():
    parser = argparse.ArgumentParser(
        description="Delete orphan/empty pages from BookStack",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--title",
        type=str,
        help="Only consider pages whose title (name) exactly matches this value "
             "(e.g. 'Introduction'). If omitted, all empty pages are candidates.",
    )
    parser.add_argument(
        "--book-id",
        type=str,
        help="Restrict to a specific BookStack book ID. If omitted, all books are scanned.",
    )
    parser.add_argument(
        "--page-id",
        type=str,
        help="Check a specific page by ID. If provided, only this page will be checked.",
    )
    parser.add_argument(
        "--slug",
        type=str,
        help="Check a specific page by slug (e.g. 'introduction-lVK'). If provided, only pages with this slug will be checked.",
    )
    parser.add_argument(
        "--min-text-length",
        type=int,
        default=10,
        help="Minimum non-whitespace text length for a page to be considered non-empty. "
             "Pages with headings, lists, images, code blocks, or tables are never considered empty. "
             "Default: 10 characters.",
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Preview deletions without actually deleting pages.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed debug output for orphan candidates (only works with --dryrun).",
    )

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("DELETE ORPHAN PAGES: Scanning BookStack for empty pages")
    print("=" * 60)

    if args.book_id:
        print(f"Restricting to book ID: {args.book_id}")
    else:
        print("Scanning ALL books")

    if args.title:
        print(f'Only considering pages with title: "{args.title}"')
    
    if args.page_id:
        print(f'Checking specific page ID: {args.page_id}')
        # Fetch just this one page
        page_detail = fetch_page_details(args.page_id)
        if not page_detail:
            print(f'Error: Could not fetch page {args.page_id}')
            return
        # Convert to list format for processing
        all_pages = [page_detail]
        original_count = 1
    else:
        # Fetch pages (optionally filtered by book)
        all_pages = fetch_bookstack_pages(book_id=args.book_id)
        original_count = len(all_pages)
    
    # Filter by slug if provided
    if args.slug:
        print(f'Filtering by slug: "{args.slug}"')
        all_pages = [p for p in all_pages if p.get("slug") == args.slug]
        if not all_pages:
            print(f'No pages found with slug "{args.slug}"')
            return

    # Deduplicate pages by ID (in case API returns duplicates across pagination)
    seen_ids = set()
    unique_pages = []
    for page in all_pages:
        page_id = page.get("id")
        if page_id and page_id not in seen_ids:
            seen_ids.add(page_id)
            unique_pages.append(page)
    
    pages = unique_pages
    total = len(pages)
    if total != original_count:
        print(f"Note: Deduplicated {original_count} pages down to {total} unique pages")
    
    candidates = 0
    deleted = 0
    skipped = 0
    errors = 0

    # Debug: Show sample page structure for first page (only with --debug)
    if pages and args.debug:
        sample = pages[0]
        print(f"\n[DEBUG] Sample page structure (first page):")
        print(f"  Keys: {list(sample.keys())}")
        print(f"  ID: {sample.get('id')}")
        print(f"  Name: {sample.get('name')}")
        print(f"  Has 'html' field: {'html' in sample}")
        if 'html' in sample:
            html_preview = (sample.get('html') or '')[:100]
            print(f"  HTML preview (first 100 chars): {repr(html_preview)}")
        print()

    for idx, page in enumerate(pages, 1):
        page_id = page.get("id")
        name = page.get("name", "")

        # Optional title filter
        if args.title and name != args.title:
            skipped += 1
            continue

        is_empty = is_page_effectively_empty(page, min_text_length=args.min_text_length)

        if not is_empty:
            skipped += 1
            continue

        # At this point the page is considered an orphan candidate.
        # Only show detailed debug info if --debug flag is used.
        if args.debug:
            html = page.get("html") or ""
            markdown = page.get("markdown") or ""
            print(f"\n[DEBUG] Orphan candidate {idx}/{total}: ID={page_id}, title='{name}'")
            print(f"  HTML length: {len(html)}, Markdown length: {len(markdown)}")
            
            # Show full HTML/markdown content (or at least more of it)
            if html:
                print(f"  HTML content: {repr(html[:500])}")
                if len(html) > 500:
                    print(f"    ... (truncated, total {len(html)} chars)")
            if markdown:
                print(f"  Markdown content: {repr(markdown[:500])}")
                if len(markdown) > 500:
                    print(f"    ... (truncated, total {len(markdown)} chars)")
            
            # Analyze why it's considered empty
            if _bs_available and html:
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Check what content elements exist
                    has_headings = bool(soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']))
                    has_lists = bool(soup.find_all(['ul', 'ol', 'li']))
                    has_images = bool(soup.find_all('img') or soup.find_all('ac:image'))
                    has_code = bool(soup.find_all(['pre', 'code']))
                    has_tables = bool(soup.find_all('table'))
                    
                    print(f"  Content analysis:")
                    print(f"    Has headings: {has_headings}")
                    print(f"    Has lists: {has_lists}")
                    print(f"    Has images: {has_images}")
                    print(f"    Has code blocks: {has_code}")
                    print(f"    Has tables: {has_tables}")
                    
                    # Get text content
                    text = soup.get_text(separator=' ', strip=True)
                    text = ' '.join(text.split())
                    print(f"    Text content length: {len(text)} (min required: {args.min_text_length})")
                    if text:
                        print(f"    Text preview: {repr(text[:200])}")
                    
                    # Check for trivial HTML
                    trivial_html = {"<p></p>", "<p><br></p>", "<p><br/></p>", "<p>&nbsp;</p>", "<p> </p>"}
                    html_lower = html.lower().strip()
                    is_trivial = html_lower in trivial_html
                    print(f"    Is trivial HTML: {is_trivial}")
                    
                except Exception as e:
                    print(f"  Error analyzing HTML: {e}")
            elif not html and not markdown:
                print(f"  Reason: Both HTML and markdown are empty/None")
            elif markdown:
                text = ' '.join(markdown.split())
                print(f"  Markdown text length: {len(text)} (min required: {args.min_text_length})")
                if text:
                    print(f"  Markdown text preview: {repr(text[:200])}")

        candidates += 1
        html_preview = (page.get("html") or "")[:50] if args.dryrun else ""
        print(f"[{idx}/{total}] Orphan page detected: ID={page_id}, title='{name}', html={repr(html_preview)}")

        if args.dryrun:
            print("  [DRYRUN] Would delete this page")
            continue

        if delete_bookstack_page(page_id, dryrun=False):
            print("  Deleted page")
            deleted += 1
            adaptive_sleep()
        else:
            print("  Error deleting page")
            errors += 1

    print("\n" + "=" * 60)
    print("DELETE ORPHAN PAGES SUMMARY")
    print("=" * 60)
    print(f"Total pages scanned: {total}")
    print(f"Orphan candidates:   {candidates}")
    print(f"Skipped (non-empty): {skipped}")
    if args.dryrun:
        print(f"[DRYRUN] Pages that WOULD be deleted: {candidates}")
    else:
        print(f"Deleted:             {deleted}")
        print(f"Errors:              {errors}")


if __name__ == "__main__":
    main()


