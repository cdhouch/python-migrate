#!/usr/bin/env python3
import os
import sys
import requests
import time
from dotenv import load_dotenv

load_dotenv()

BOOKSTACK_BASE_URL = os.getenv('BOOKSTACK_HOST')
BOOKSTACK_TOKEN_ID = os.getenv('BOOKSTACK_TOKEN_ID')
BOOKSTACK_TOKEN_SECRET = os.getenv('BOOKSTACK_TOKEN_SECRET')

headers = {'Authorization': f'Token {BOOKSTACK_TOKEN_ID}:{BOOKSTACK_TOKEN_SECRET}'}

# Adaptive rate limiting - starts fast, slows down if we hit rate limits
_rate_limit_delay = 0.1  # Start with 0.1 second delay (10 requests/second)
_max_rate_limit_delay = 1.0  # Maximum delay if we keep hitting rate limits

def adaptive_sleep():
    """Sleep with adaptive delay based on rate limiting."""
    global _rate_limit_delay
    time.sleep(_rate_limit_delay)

def handle_rate_limit():
    """Handle rate limit by increasing delay and waiting."""
    global _rate_limit_delay
    _rate_limit_delay = min(_rate_limit_delay * 1.5, _max_rate_limit_delay)
    print(f'  Rate limited, increasing delay to {_rate_limit_delay:.2f}s...')
    time.sleep(2)  # Wait before retry

def delete_book(book_id):
    """Delete a book from BookStack."""
    url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
    response = requests.delete(url, headers=headers)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.delete(url, headers=headers)
    if response.status_code in [200, 204]:
        return True
    else:
        print(f'  Error deleting book {book_id}: {response.status_code} - {response.text}')
        return False

def delete_shelf(shelf_id):
    """Delete a shelf from BookStack."""
    url = f'{BOOKSTACK_BASE_URL}/api/shelves/{shelf_id}'
    response = requests.delete(url, headers=headers)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.delete(url, headers=headers)
    if response.status_code in [200, 204]:
        return True
    else:
        print(f'  Error deleting shelf {shelf_id}: {response.status_code} - {response.text}')
        return False

# Delete all pages and chapters from all books first
print("Step 1: Deleting all pages and chapters from all books...")
from migrate import delete_all_bookstack_pages

# Get all books
print("  Fetching all books...")
books = []
page_num = 1
while True:
    url = f'{BOOKSTACK_BASE_URL}/api/books'
    params = {'count': 100, 'page': page_num}
    response = requests.get(url, headers=headers, params=params)
    data = response.json()
    books.extend(data.get('data', []))
    if len(data.get('data', [])) < 100:
        break
    page_num += 1
    print(f"    Fetched {len(books)} books so far...")

print(f"  Found {len(books)} books")
for idx, book in enumerate(books, 1):
    book_id = book.get('id')
    book_name = book.get('name')
    print(f"  [{idx}/{len(books)}] Deleting content from book {book_id}: {book_name}...")
    delete_all_bookstack_pages(book_id=book_id, dryrun=False)

# Check for any shelves and delete them
print("\nStep 3: Deleting all shelves...")
url = f'{BOOKSTACK_BASE_URL}/api/shelves'
response = requests.get(url, headers=headers)
shelves = response.json().get('data', [])
print(f"Found {len(shelves)} shelves")
for idx, shelf in enumerate(shelves, 1):
    shelf_id = shelf.get('id')
    shelf_name = shelf.get('name')
    print(f"  [{idx}/{len(shelves)}] Deleting shelf {shelf_id}: {shelf_name}...")
    if delete_shelf(shelf_id):
        print(f"    Successfully deleted shelf {shelf_id}")
    else:
        print(f"    Failed to delete shelf {shelf_id}")
    adaptive_sleep()  # Adaptive rate limit protection

# Delete all books (now that their content is deleted)
print("\nStep 2: Deleting all books...")
# Re-fetch books in case new ones were created
books = []
page_num = 1
while True:
    url = f'{BOOKSTACK_BASE_URL}/api/books'
    params = {'count': 100, 'page': page_num}
    response = requests.get(url, headers=headers, params=params)
    data = response.json()
    books.extend(data.get('data', []))
    if len(data.get('data', [])) < 100:
        break
    page_num += 1
    print(f"  Fetched {len(books)} books so far...")

print(f"Found {len(books)} books")
for idx, book in enumerate(books, 1):
    book_id = book.get('id')
    book_name = book.get('name')
    print(f"  [{idx}/{len(books)}] Deleting book {book_id}: {book_name}...")
    if delete_book(book_id):
        print(f"    Successfully deleted book {book_id}")
    else:
        print(f"    Failed to delete book {book_id}")
    adaptive_sleep()  # Adaptive rate limit protection

print("\nDeletion complete!")

