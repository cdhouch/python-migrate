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

def delete_book(book_id):
    """Delete a book from BookStack."""
    url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
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
    if response.status_code in [200, 204]:
        return True
    else:
        print(f'  Error deleting shelf {shelf_id}: {response.status_code} - {response.text}')
        return False

# First, delete all pages and chapters from book 1
print("Step 1: Deleting all pages and chapters from book 1...")
from migrate import delete_all_bookstack_pages
delete_all_bookstack_pages(book_id=1, dryrun=False)

# Then delete the book itself
print("\nStep 2: Deleting book 'Wicked Fox Games' (ID: 1)...")
if delete_book(1):
    print("  Successfully deleted book 1")
else:
    print("  Failed to delete book 1")

# Check for any shelves and delete them
print("\nStep 3: Checking for shelves...")
url = f'{BOOKSTACK_BASE_URL}/api/shelves'
response = requests.get(url, headers=headers)
shelves = response.json().get('data', [])
print(f"Found {len(shelves)} shelves")
for shelf in shelves:
    shelf_id = shelf.get('id')
    shelf_name = shelf.get('name')
    print(f"  Deleting shelf {shelf_id}: {shelf_name}...")
    if delete_shelf(shelf_id):
        print(f"    Successfully deleted shelf {shelf_id}")
    else:
        print(f"    Failed to delete shelf {shelf_id}")
    time.sleep(0.5)

print("\nDeletion complete!")

