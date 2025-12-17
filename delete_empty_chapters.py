#!/usr/bin/env python3
"""
Utility script: Delete chapters from BookStack that have no pages.

Usage examples:

    # Dry run: list all empty chapters (any book)
    python delete_empty_chapters.py --dryrun

    # Delete empty chapters only within a specific book
    python delete_empty_chapters.py --book-id 6690 --dryrun

    # Delete ALL empty chapters across all books (be careful!)
    python delete_empty_chapters.py --dryrun
    python delete_empty_chapters.py
"""

import argparse

from migrate import (
    fetch_bookstack_books,
    fetch_bookstack_chapters,
    fetch_bookstack_pages,
    delete_bookstack_chapter,
    adaptive_sleep,
)


def find_empty_chapters(book_id=None, debug=False):
    """
    Find all chapters that have no pages.
    
    Args:
        book_id: Optional book ID to restrict search to
        debug: If True, show debug output
    
    Returns:
        List of chapter dictionaries that are empty
    """
    empty_chapters = []
    
    if book_id:
        books = [b for b in fetch_bookstack_books() if str(b.get('id')) == str(book_id)]
        if not books:
            print(f'Book ID {book_id} not found')
            return []
    else:
        books = fetch_bookstack_books()
    
    print(f'Scanning {len(books)} book(s) for empty chapters...')
    
    # Fetch all pages once to build a lookup map
    print('Fetching all pages to check chapter membership...')
    all_pages = fetch_bookstack_pages(book_id=book_id)
    
    # Build a set of chapter IDs that have pages
    chapters_with_pages = set()
    for page in all_pages:
        chapter_id = page.get('chapter_id')
        if chapter_id:
            chapters_with_pages.add(chapter_id)
    
    print(f'Found {len(chapters_with_pages)} chapters that have pages')
    
    # Now check each book's chapters
    for book in books:
        book_id = book.get('id')
        book_name = book.get('name', 'Unknown')
        
        if debug:
            print(f'\nChecking book: {book_name} (ID: {book_id})')
        
        chapters = fetch_bookstack_chapters(book_id)
        
        if debug:
            print(f'  Found {len(chapters)} chapters in this book')
        
        for chapter in chapters:
            chapter_id = chapter.get('id')
            chapter_name = chapter.get('name', 'Unknown')
            
            if chapter_id not in chapters_with_pages:
                empty_chapters.append(chapter)
                if debug:
                    print(f'  [EMPTY] Chapter ID={chapter_id}, name="{chapter_name}"')
            elif debug:
                print(f'  [HAS PAGES] Chapter ID={chapter_id}, name="{chapter_name}"')
    
    return empty_chapters


def main():
    parser = argparse.ArgumentParser(
        description="Delete chapters from BookStack that have no pages",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--book-id",
        type=str,
        help="Restrict to a specific BookStack book ID. If omitted, all books are scanned.",
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Preview deletions without actually deleting chapters.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed debug output.",
    )

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("DELETE EMPTY CHAPTERS: Scanning BookStack for chapters with no pages")
    print("=" * 60)

    if args.book_id:
        print(f"Restricting to book ID: {args.book_id}")
    else:
        print("Scanning ALL books")

    # Find empty chapters
    empty_chapters = find_empty_chapters(book_id=args.book_id, debug=args.debug)
    
    total = len(empty_chapters)
    deleted = 0
    errors = 0

    if total == 0:
        print("\nNo empty chapters found!")
        return

    print(f"\nFound {total} empty chapter(s)")

    # Delete empty chapters
    for idx, chapter in enumerate(empty_chapters, 1):
        chapter_id = chapter.get("id")
        chapter_name = chapter.get("name", "Unknown")
        book_id = chapter.get("book_id")
        book_name = chapter.get("book_name", "Unknown")

        print(f"[{idx}/{total}] Empty chapter detected: ID={chapter_id}, name='{chapter_name}', book='{book_name}' (ID: {book_id})")

        if args.dryrun:
            print("  [DRYRUN] Would delete this chapter")
            continue

        if delete_bookstack_chapter(chapter_id, dryrun=False):
            print("  Deleted chapter")
            deleted += 1
            adaptive_sleep()
        else:
            print("  Error deleting chapter")
            errors += 1

    print("\n" + "=" * 60)
    print("DELETE EMPTY CHAPTERS SUMMARY")
    print("=" * 60)
    print(f"Total empty chapters found: {total}")
    if args.dryrun:
        print(f"[DRYRUN] Chapters that WOULD be deleted: {total}")
    else:
        print(f"Deleted:             {deleted}")
        print(f"Errors:              {errors}")


if __name__ == "__main__":
    main()

