#!/usr/bin/env python3
"""
Migration Tool - Jira to OpenProject / Confluence to BookStack

A Python-based migration tool that handles:
- Jira to OpenProject: Syncing issues and assigning children to parent Epics
- Confluence to BookStack: Syncing pages and maintaining page hierarchies

Usage:
    # Jira to OpenProject
    python migrate.py jira --sync-issues [--dryrun]
    python migrate.py jira --assign-epics [--dryrun]
    
    # Confluence to BookStack
    python migrate.py confluence --sync-pages [--dryrun]
    python migrate.py confluence --sync-spaces [--dryrun]
"""

import requests
import json
from dotenv import load_dotenv
import os
import argparse
import tempfile
import shutil
from fuzzywuzzy import fuzz
from enum import Enum

load_dotenv()

# =============================================================================
# Migration Type Enum
# =============================================================================

class MigrationType(Enum):
    JIRA_TO_OPENPROJECT = "jira"
    CONFLUENCE_TO_BOOKSTACK = "confluence"


# =============================================================================
# Configuration - Jira to OpenProject
# =============================================================================

# Jira credentials
JIRA_BASE_URL = os.getenv('JIRA_HOST')  # e.g., 'yourcompany.atlassian.net'
JIRA_EMAIL = os.getenv('JIRA_EMAIL')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY', 'ROE')

# OpenProject credentials
OP_BASE_URL = os.getenv('OPENPROJECT_HOST')  # e.g., 'https://openproject.example.com'
OP_API_KEY = os.getenv('OPENPROJECT_API_KEY')
OP_PROJECT_ID = int(os.getenv('OPENPROJECT_PROJECT_ID', '3'))
JIRA_ID_CUSTOM_FIELD = int(os.getenv('JIRA_ID_CUSTOM_FIELD', '1'))

# Authentication
jira_auth = (JIRA_EMAIL, JIRA_API_TOKEN)
op_auth = ('apikey', OP_API_KEY)

# Type mappings (Jira -> OpenProject)
TYPE_MAPPING = {
    'Task': 'Task',
    'Story': 'User story',
    'Bug': 'Bug',
    'Epic': 'Epic',
    'Feature': 'Feature',
    'Milestone': 'Milestone',
    'Sub-task': 'Task',
}

# Status mappings (Jira -> OpenProject)
STATUS_MAPPING = {
    'To Do': 'New',
    'In Progress': 'In progress',
    'Done': 'Closed',
    'Closed': 'Closed',
    'Resolved': 'Closed',
}

# Priority mappings (Jira -> OpenProject)
PRIORITY_MAPPING = {
    'Highest': 'Immediate',
    'High': 'High',
    'Medium': 'Normal',
    'Low': 'Low',
    'Lowest': 'Low',
}

# Cache for OpenProject metadata
_op_types_cache = None
_op_statuses_cache = None
_op_priorities_cache = None


# =============================================================================
# Configuration - Confluence to BookStack
# =============================================================================

# Confluence credentials
CONFLUENCE_BASE_URL = os.getenv('CONFLUENCE_HOST')  # e.g., 'yourcompany.atlassian.net'
CONFLUENCE_EMAIL = os.getenv('CONFLUENCE_EMAIL')
CONFLUENCE_API_TOKEN = os.getenv('CONFLUENCE_API_TOKEN')
CONFLUENCE_SPACE_KEY = os.getenv('CONFLUENCE_SPACE_KEY')  # e.g., 'SPACE'

# BookStack credentials
BOOKSTACK_BASE_URL = os.getenv('BOOKSTACK_HOST')  # e.g., 'https://bookstack.example.com'
BOOKSTACK_TOKEN_ID = os.getenv('BOOKSTACK_TOKEN_ID')
BOOKSTACK_TOKEN_SECRET = os.getenv('BOOKSTACK_TOKEN_SECRET')
BOOKSTACK_BOOK_ID = os.getenv('BOOKSTACK_BOOK_ID')  # Optional: specific book ID

# Authentication
confluence_auth = (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)
bookstack_auth = (BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET)

# Cache for BookStack metadata
_bookstack_books_cache = None
_bookstack_shelves_cache = None


# =============================================================================
# Jira API Functions
# =============================================================================

def fetch_jira_issues(project_key=None, issue_type=None, specific_keys=None):
    """Fetch issues from Jira with optional filters."""
    project_key = project_key or JIRA_PROJECT_KEY
    url = f'https://{JIRA_BASE_URL}/rest/api/3/search/jql'
    
    if specific_keys:
        jql = f'key in ({",".join(specific_keys)})'
    elif issue_type:
        jql = f'project = {project_key} AND issuetype = {issue_type} ORDER BY created ASC'
    else:
        jql = f'project = {project_key} ORDER BY created ASC'
    
    issues = []
    next_token = None
    page = 1
    
    while True:
        print(f'Fetching Jira issues page {page}...')
        payload = {
            'jql': jql,
            'fields': [
                'key', 'summary', 'description', 'status', 'priority',
                'issuetype', 'attachment', 'comment', 'assignee', 'creator',
                'created', 'customfield_10014', 'parent'  # customfield_10014 = Epic Link
            ],
            'expand': 'renderedFields',
            'maxResults': 100,
        }
        if next_token:
            payload['nextPageToken'] = next_token
        
        response = requests.post(url, auth=jira_auth, json=payload)
        response.raise_for_status()
        data = response.json()
        
        issues.extend(data['issues'])
        next_token = data.get('nextPageToken')
        
        if not next_token:
            break
        page += 1
    
    print(f'Found {len(issues)} issues in Jira.')
    return issues


def fetch_jira_epics():
    """Fetch all Epics from Jira project."""
    issues = fetch_jira_issues(issue_type='Epic')
    epics_dict = {issue['key']: issue['fields']['summary'] for issue in issues}
    return epics_dict


def fetch_jira_children(epic_key):
    """Fetch child issues for a Jira Epic."""
    jql = f'"Epic Link" = {epic_key} ORDER BY created ASC'
    url = f'https://{JIRA_BASE_URL}/rest/api/3/search/jql'
    children = []
    next_token = None
    
    while True:
        payload = {
            'jql': jql,
            'fields': ['key', 'summary'],
            'maxResults': 100,
        }
        if next_token:
            payload['nextPageToken'] = next_token
        
        response = requests.post(url, auth=jira_auth, json=payload)
        response.raise_for_status()
        data = response.json()
        children.extend(data['issues'])
        next_token = data.get('nextPageToken')
        
        if not next_token:
            break
    
    children_dict = {issue['key']: issue['fields']['summary'] for issue in children}
    print(f'Found {len(children_dict)} children for Epic {epic_key}.')
    return children_dict


# =============================================================================
# OpenProject API Functions
# =============================================================================

def fetch_op_work_packages(project_id=None):
    """Fetch all work packages in OpenProject project, with pagination."""
    project_id = project_id or OP_PROJECT_ID
    work_packages = []
    page = 1
    page_size = 100
    total = None
    
    # Use filters approach for complete results (projects endpoint may filter)
    filters = json.dumps([{'project': {'operator': '=', 'values': [str(project_id)]}}])
    
    while True:
        url = f'{OP_BASE_URL}/api/v3/work_packages?offset={page}&pageSize={page_size}&filters={filters}'
        print(f'Fetching OpenProject work packages page {page}...')
        response = requests.get(url, auth=op_auth)
        response.raise_for_status()
        data = response.json()
        
        if total is None:
            total = data.get('total', 0)
            print(f'Total work packages to fetch: {total}')
        
        elements = data['_embedded']['elements']
        work_packages.extend(elements)
        
        if total > 0:
            print(f'Retrieved {len(work_packages)} of {total} work packages ({round(len(work_packages) / total * 100)}%)')
        
        if len(work_packages) >= total or len(elements) == 0:
            break
        
        page += 1
    
    print(f'Found {len(work_packages)} work packages in OpenProject.')
    return work_packages


def fetch_op_types():
    """Fetch work package types from OpenProject."""
    global _op_types_cache
    if _op_types_cache:
        return _op_types_cache
    
    url = f'{OP_BASE_URL}/api/v3/types'
    response = requests.get(url, auth=op_auth)
    response.raise_for_status()
    _op_types_cache = {t['name']: t['id'] for t in response.json()['_embedded']['elements']}
    print(f'Loaded {len(_op_types_cache)} work package types.')
    return _op_types_cache


def fetch_op_statuses():
    """Fetch work package statuses from OpenProject."""
    global _op_statuses_cache
    if _op_statuses_cache:
        return _op_statuses_cache
    
    url = f'{OP_BASE_URL}/api/v3/statuses'
    response = requests.get(url, auth=op_auth)
    response.raise_for_status()
    _op_statuses_cache = {s['name']: s['id'] for s in response.json()['_embedded']['elements']}
    print(f'Loaded {len(_op_statuses_cache)} work package statuses.')
    return _op_statuses_cache


def fetch_op_priorities():
    """Fetch work package priorities from OpenProject."""
    global _op_priorities_cache
    if _op_priorities_cache:
        return _op_priorities_cache
    
    url = f'{OP_BASE_URL}/api/v3/priorities'
    response = requests.get(url, auth=op_auth)
    response.raise_for_status()
    _op_priorities_cache = {p['name']: p['id'] for p in response.json()['_embedded']['elements']}
    print(f'Loaded {len(_op_priorities_cache)} work package priorities.')
    return _op_priorities_cache


def get_op_type_id(jira_type):
    """Get OpenProject type ID for a Jira issue type."""
    types = fetch_op_types()
    mapped_type = TYPE_MAPPING.get(jira_type, 'Task')
    type_id = types.get(mapped_type)
    if not type_id:
        # Fallback to first available type
        type_id = list(types.values())[0]
        print(f'Warning: Type "{mapped_type}" not found, using default.')
    return type_id


def get_op_status_id(jira_status):
    """Get OpenProject status ID for a Jira status."""
    statuses = fetch_op_statuses()
    mapped_status = STATUS_MAPPING.get(jira_status, 'New')
    status_id = statuses.get(mapped_status)
    if not status_id:
        # Fallback to first available status
        status_id = list(statuses.values())[0]
        print(f'Warning: Status "{mapped_status}" not found, using default.')
    return status_id


def get_op_priority_id(jira_priority):
    """Get OpenProject priority ID for a Jira priority."""
    priorities = fetch_op_priorities()
    if not jira_priority:
        return priorities.get('Normal', list(priorities.values())[0])
    
    mapped_priority = PRIORITY_MAPPING.get(jira_priority, 'Normal')
    priority_id = priorities.get(mapped_priority)
    if not priority_id:
        priority_id = list(priorities.values())[0]
        print(f'Warning: Priority "{mapped_priority}" not found, using default.')
    return priority_id


def find_op_work_package_by_jira_id(jira_key, project_id=None):
    """Find an OpenProject work package by Jira ID custom field."""
    project_id = project_id or OP_PROJECT_ID
    url = f'{OP_BASE_URL}/api/v3/work_packages'
    
    filters = json.dumps([
        {'project': {'operator': '=', 'values': [str(project_id)]}},
        {f'customField{JIRA_ID_CUSTOM_FIELD}': {'operator': '=', 'values': [jira_key]}}
    ])
    
    response = requests.get(url, auth=op_auth, params={'filters': filters})
    response.raise_for_status()
    
    elements = response.json()['_embedded']['elements']
    return elements[0] if elements else None


def get_lock_version(wp_id):
    """Get current lockVersion for a work package."""
    url = f'{OP_BASE_URL}/api/v3/work_packages/{wp_id}'
    response = requests.get(url, auth=op_auth)
    response.raise_for_status()
    return response.json()['lockVersion']


def create_work_package(payload, project_id=None):
    """Create a new work package in OpenProject."""
    project_id = project_id or OP_PROJECT_ID
    url = f'{OP_BASE_URL}/api/v3/work_packages'
    
    payload['_links']['project'] = {'href': f'/api/v3/projects/{project_id}'}
    
    response = requests.post(url, auth=op_auth, json=payload)
    if response.status_code == 201:
        return response.json()
    else:
        print(f'Error creating work package: {response.status_code}')
        print(response.text)
        return None


def update_work_package(wp_id, payload):
    """Update an existing work package in OpenProject."""
    url = f'{OP_BASE_URL}/api/v3/work_packages/{wp_id}'
    
    # Get lock version
    payload['lockVersion'] = get_lock_version(wp_id)
    
    response = requests.patch(url, auth=op_auth, json=payload)
    if response.status_code == 200:
        return response.json()
    else:
        print(f'Error updating work package {wp_id}: {response.status_code}')
        print(response.text)
        return None


def set_work_package_parent(child_id, parent_id, dryrun=False):
    """Set parent for a work package in OpenProject."""
    if dryrun:
        print(f'[DRYRUN] Would set parent of WP {child_id} to {parent_id}.')
        return True
    
    url = f'{OP_BASE_URL}/api/v3/work_packages/{child_id}'
    payload = {
        'lockVersion': get_lock_version(child_id),
        '_links': {
            'parent': {'href': f'/api/v3/work_packages/{parent_id}'}
        }
    }
    
    response = requests.patch(url, auth=op_auth, json=payload)
    if response.status_code == 200:
        print(f'Set parent of WP {child_id} to {parent_id}.')
        return True
    else:
        print(f'Error setting parent for WP {child_id}: {response.text}')
        return False


# =============================================================================
# Confluence API Functions
# =============================================================================

def fetch_confluence_spaces(space_key=None):
    """Fetch spaces from Confluence. If space_key is provided, fetch only that space."""
    if space_key:
        url = f'https://{CONFLUENCE_BASE_URL}/rest/api/space/{space_key}'
        response = requests.get(url, auth=confluence_auth)
        response.raise_for_status()
        return [response.json()]
    else:
        url = f'https://{CONFLUENCE_BASE_URL}/rest/api/space'
        spaces = []
        start = 0
        limit = 50
        
        while True:
            params = {'start': start, 'limit': limit}
            response = requests.get(url, auth=confluence_auth, params=params)
            response.raise_for_status()
            data = response.json()
            
            spaces.extend(data.get('results', []))
            
            if len(data.get('results', [])) < limit:
                break
            start += limit
        
        print(f'Found {len(spaces)} spaces in Confluence.')
        return spaces


def fetch_confluence_pages(space_key=None, page_id=None, expand='body.storage,version,ancestors'):
    """Fetch pages from Confluence. Can fetch all pages in a space or a specific page."""
    if page_id:
        url = f'https://{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}'
        params = {'expand': expand}
        response = requests.get(url, auth=confluence_auth, params=params)
        response.raise_for_status()
        return [response.json()]
    
    url = f'https://{CONFLUENCE_BASE_URL}/rest/api/content'
    pages = []
    start = 0
    limit = 50
    
    params = {
        'start': start,
        'limit': limit,
        'expand': expand,
        'type': 'page'
    }
    
    if space_key:
        params['spaceKey'] = space_key
    
    while True:
        params['start'] = start
        response = requests.get(url, auth=confluence_auth, params=params)
        response.raise_for_status()
        data = response.json()
        
        pages.extend(data.get('results', []))
        
        if len(data.get('results', [])) < limit:
            break
        start += limit
        
        print(f'Fetched {len(pages)} pages so far...')
    
    print(f'Found {len(pages)} pages in Confluence.')
    return pages


def fetch_confluence_page_children(page_id, expand='body.storage,version'):
    """Fetch child pages of a specific Confluence page."""
    url = f'https://{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}/child/page'
    params = {'expand': expand, 'limit': 50}
    children = []
    start = 0
    
    while True:
        params['start'] = start
        response = requests.get(url, auth=confluence_auth, params=params)
        response.raise_for_status()
        data = response.json()
        
        children.extend(data.get('results', []))
        
        if len(data.get('results', [])) < 50:
            break
        start += 50
    
    return children


def convert_atlassian_storage_to_html(storage):
    """Convert Atlassian Document Format (storage format) to HTML."""
    if not storage:
        return ''
    if isinstance(storage, str):
        return storage
    
    # If it's already HTML, return as-is
    if isinstance(storage, dict) and 'value' in storage:
        return storage.get('value', '')
    
    return str(storage)


# =============================================================================
# BookStack API Functions
# =============================================================================

def fetch_bookstack_shelves():
    """Fetch all shelves from BookStack."""
    global _bookstack_shelves_cache
    if _bookstack_shelves_cache:
        return _bookstack_shelves_cache
    
    url = f'{BOOKSTACK_BASE_URL}/api/shelves'
    response = requests.get(url, auth=bookstack_auth)
    response.raise_for_status()
    _bookstack_shelves_cache = {s['name']: s['id'] for s in response.json()['data']}
    print(f'Loaded {len(_bookstack_shelves_cache)} shelves from BookStack.')
    return _bookstack_shelves_cache


def fetch_bookstack_books(book_id=None):
    """Fetch books from BookStack. If book_id is provided, fetch only that book."""
    global _bookstack_books_cache
    
    if book_id:
        url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
        response = requests.get(url, auth=bookstack_auth)
        response.raise_for_status()
        return [response.json()]
    
    url = f'{BOOKSTACK_BASE_URL}/api/books'
    books = []
    page = 1
    per_page = 100
    
    while True:
        params = {'count': per_page, 'page': page}
        response = requests.get(url, auth=bookstack_auth, params=params)
        response.raise_for_status()
        data = response.json()
        
        books.extend(data.get('data', []))
        
        if len(data.get('data', [])) < per_page:
            break
        page += 1
        print(f'Fetched {len(books)} books so far...')
    
    print(f'Found {len(books)} books in BookStack.')
    
    # Update cache
    if not _bookstack_books_cache:
        _bookstack_books_cache = {}
    for book in books:
        _bookstack_books_cache[book['name']] = book['id']
    
    return books


def fetch_bookstack_pages(book_id=None):
    """Fetch pages from BookStack. If book_id is provided, filter by that book."""
    url = f'{BOOKSTACK_BASE_URL}/api/pages'
    pages = []
    page = 1
    per_page = 100
    
    while True:
        params = {'count': per_page, 'page': page}
        if book_id:
            params['book_id'] = book_id
        
        response = requests.get(url, auth=bookstack_auth, params=params)
        response.raise_for_status()
        data = response.json()
        
        pages.extend(data.get('data', []))
        
        if len(data.get('data', [])) < per_page:
            break
        page += 1
        print(f'Fetched {len(pages)} pages so far...')
    
    print(f'Found {len(pages)} pages in BookStack.')
    return pages


def find_bookstack_page_by_confluence_id(confluence_id, book_id=None):
    """Find a BookStack page by Confluence ID (stored in page name or markdown)."""
    pages = fetch_bookstack_pages(book_id)
    
    # Look for Confluence ID in page HTML or name
    for page in pages:
        # Check if Confluence ID is in the page content or name
        # This is a simple implementation - you might need to store it differently
        if confluence_id in page.get('name', '') or confluence_id in page.get('html', ''):
            return page
    
    return None


def create_bookstack_book(name, description='', shelf_id=None, dryrun=False):
    """Create a new book in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would create book: {name}')
        return {'id': 'dryrun_id', 'name': name}
    
    url = f'{BOOKSTACK_BASE_URL}/api/books'
    payload = {
        'name': name,
        'description': description or '',
    }
    
    if shelf_id:
        payload['shelf_id'] = shelf_id
    
    response = requests.post(url, auth=bookstack_auth, json=payload)
    if response.status_code in [200, 201]:
        return response.json()
    else:
        print(f'Error creating book: {response.status_code}')
        print(response.text)
        return None


def create_bookstack_page(name, html, book_id, chapter_id=None, parent_id=None, dryrun=False):
    """Create a new page in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would create page: {name} in book {book_id}')
        return {'id': 'dryrun_id', 'name': name}
    
    url = f'{BOOKSTACK_BASE_URL}/api/pages'
    payload = {
        'name': name,
        'html': html,
        'book_id': book_id,
    }
    
    if chapter_id:
        payload['chapter_id'] = chapter_id
    if parent_id:
        payload['parent_id'] = parent_id
    
    response = requests.post(url, auth=bookstack_auth, json=payload)
    if response.status_code in [200, 201]:
        return response.json()
    else:
        print(f'Error creating page: {response.status_code}')
        print(response.text)
        return None


def update_bookstack_page(page_id, name=None, html=None, dryrun=False):
    """Update an existing page in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would update page {page_id}')
        return True
    
    url = f'{BOOKSTACK_BASE_URL}/api/pages/{page_id}'
    
    # Get current page data
    response = requests.get(url, auth=bookstack_auth)
    if response.status_code != 200:
        print(f'Error fetching page {page_id}: {response.status_code}')
        return False
    
    current_data = response.json()
    payload = {}
    
    if name is not None:
        payload['name'] = name
    if html is not None:
        payload['html'] = html
    
    if not payload:
        return True
    
    # Include required fields
    payload['book_id'] = current_data.get('book_id')
    
    response = requests.put(url, auth=bookstack_auth, json=payload)
    if response.status_code in [200, 201]:
        return True
    else:
        print(f'Error updating page {page_id}: {response.status_code}')
        print(response.text)
        return False


# =============================================================================
# Migration Functions - Jira to OpenProject
# =============================================================================

def convert_atlassian_doc_to_text(doc):
    """Convert Atlassian Document Format to plain text."""
    if not doc:
        return ''
    if isinstance(doc, str):
        return doc
    
    try:
        if 'content' in doc:
            paragraphs = []
            for block in doc['content']:
                if 'content' in block:
                    text = ''.join(c.get('text', '') for c in block['content'])
                    paragraphs.append(text)
            return '\n'.join(paragraphs).strip()
        return ''
    except Exception as e:
        print(f'Error converting Atlassian document: {e}')
        return ''


def sync_jira_issues(dryrun=False, skip_existing=True, specific_keys=None):
    """Sync issues from Jira to OpenProject."""
    print('\n' + '=' * 60)
    print('SYNC ISSUES: Migrating from Jira to OpenProject')
    print('=' * 60)
    
    # Fetch Jira issues
    if specific_keys:
        jira_issues = fetch_jira_issues(specific_keys=specific_keys)
    else:
        jira_issues = fetch_jira_issues()
    
    # Load OpenProject metadata
    fetch_op_types()
    fetch_op_statuses()
    fetch_op_priorities()
    
    # Build cache of existing work packages by Jira ID
    print('\nBuilding cache of existing OpenProject work packages...')
    op_work_packages = fetch_op_work_packages()
    existing_by_jira_id = {}
    for wp in op_work_packages:
        jira_id = wp.get(f'customField{JIRA_ID_CUSTOM_FIELD}')
        if jira_id:
            existing_by_jira_id[jira_id] = wp
    print(f'Found {len(existing_by_jira_id)} existing work packages with Jira IDs.')
    
    # Process issues
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    
    for issue in jira_issues:
        jira_key = issue['key']
        fields = issue['fields']
        
        print(f'\nProcessing {jira_key}: {fields["summary"][:50]}...')
        
        # Check if already exists
        existing_wp = existing_by_jira_id.get(jira_key)
        
        if existing_wp and skip_existing:
            print(f'  Skipping - already exists as WP {existing_wp["id"]}')
            skipped += 1
            continue
        
        # Build payload
        description = convert_atlassian_doc_to_text(fields.get('description'))
        
        # Get rendered description if available
        if 'renderedFields' in issue and issue['renderedFields'].get('description'):
            description = issue['renderedFields']['description']
        
        payload = {
            '_type': 'WorkPackage',
            'subject': fields['summary'],
            'description': {
                'raw': description
            },
            f'customField{JIRA_ID_CUSTOM_FIELD}': jira_key,
            '_links': {
                'type': {
                    'href': f'/api/v3/types/{get_op_type_id(fields["issuetype"]["name"])}'
                },
                'status': {
                    'href': f'/api/v3/statuses/{get_op_status_id(fields["status"]["name"])}'
                },
                'priority': {
                    'href': f'/api/v3/priorities/{get_op_priority_id(fields.get("priority", {}).get("name"))}'
                }
            }
        }
        
        if dryrun:
            if existing_wp:
                print(f'  [DRYRUN] Would update WP {existing_wp["id"]}')
                updated += 1
            else:
                print(f'  [DRYRUN] Would create new work package')
                created += 1
            continue
        
        try:
            if existing_wp:
                # Update existing
                result = update_work_package(existing_wp['id'], payload)
                if result:
                    print(f'  Updated WP {existing_wp["id"]}')
                    updated += 1
                else:
                    errors += 1
            else:
                # Create new
                result = create_work_package(payload)
                if result:
                    print(f'  Created WP {result["id"]}')
                    created += 1
                    # Add to cache for parent assignment later
                    existing_by_jira_id[jira_key] = result
                else:
                    errors += 1
        except Exception as e:
            print(f'  Error: {e}')
            errors += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('SYNC ISSUES SUMMARY')
    print('=' * 60)
    print(f'Total processed: {len(jira_issues)}')
    print(f'Created: {created}')
    print(f'Updated: {updated}')
    print(f'Skipped: {skipped}')
    print(f'Errors: {errors}')


def build_op_mapping(work_packages, jira_items, type_filter=None, diagnose=False):
    """Map Jira keys to OpenProject IDs by Jira ID custom field first, then fuzzy matching."""
    mapping = {}
    unmatched = []
    op_candidates = []
    
    # Build index by Jira ID custom field for exact matching
    wp_by_jira_id = {}
    for wp in work_packages:
        jira_id = wp.get(f'customField{JIRA_ID_CUSTOM_FIELD}')
        if jira_id:
            wp_by_jira_id[jira_id] = wp
    
    # Get Epic type ID if filtering by Epic
    epic_type_id = None
    if type_filter == 'Epic':
        types = fetch_op_types()
        epic_type_id = types.get('Epic')
    
    for wp in work_packages:
        # Extract type ID from _links.type.href
        type_href = wp['_links']['type']['href']
        type_id = int(type_href.split('/')[-1]) if type_href else None
        
        if type_filter == 'Epic' and epic_type_id:
            if type_id == epic_type_id:
                op_candidates.append(wp)
        else:
            op_candidates.append(wp)
    
    if diagnose and type_filter == 'Epic':
        print(f'\n=== All OpenProject Epics ({len(op_candidates)} total) ===')
        for wp in op_candidates:
            jira_id = wp.get(f'customField{JIRA_ID_CUSTOM_FIELD}', 'N/A')
            print(f'  OP {wp["id"]} (Jira: {jira_id}): "{wp["subject"]}"')
        print('=' * 50 + '\n')
    
    for jira_key, jira_summary in jira_items.items():
        # First try exact match by Jira ID custom field
        if jira_key in wp_by_jira_id:
            wp = wp_by_jira_id[jira_key]
            # Check type filter if applicable
            if type_filter == 'Epic' and epic_type_id:
                type_href = wp['_links']['type']['href']
                type_id = int(type_href.split('/')[-1]) if type_href else None
                if type_id != epic_type_id:
                    print(f'Jira {jira_key} found by ID but wrong type (not Epic), skipping.')
                    continue
            mapping[jira_key] = wp['id']
            print(f'Mapped Jira {jira_key} ("{jira_summary}") to OP {wp["id"]} (exact Jira ID match).')
            continue
        
        # Fallback to fuzzy matching
        best_match_id = None
        best_match_subject = None
        best_score = 0
        top_matches = []
        
        for wp in op_candidates:
            score = fuzz.token_sort_ratio(jira_summary, wp['subject'])
            if diagnose:
                top_matches.append((score, wp['id'], wp['subject']))
            if score > best_score and score > 90:
                best_score = score
                best_match_id = wp['id']
                best_match_subject = wp['subject']
        
        if best_match_id:
            mapping[jira_key] = best_match_id
            print(f'Mapped Jira {jira_key} ("{jira_summary}") to OP {best_match_id} (fuzzy score {best_score}).')
        else:
            print(f'No match found for Jira {jira_key} ("{jira_summary}").')
            if diagnose:
                top_matches.sort(reverse=True, key=lambda x: x[0])
                unmatched.append((jira_key, jira_summary, top_matches[:3]))
    
    print(f'Mapped {len(mapping)} Jira items to OpenProject IDs.')
    
    if diagnose and unmatched:
        print(f'\n=== Diagnostic: Unmatched Jira Items ({len(unmatched)}) ===')
        for jira_key, jira_summary, top_matches in unmatched:
            print(f'\nJira {jira_key}: "{jira_summary}"')
            if top_matches:
                print('  Closest matches in OpenProject:')
                for score, op_id, op_subject in top_matches:
                    print(f'    Score {score}: OP {op_id} - "{op_subject}"')
            else:
                print('  No candidates found!')
        print('=' * 50 + '\n')
    
    return mapping


def assign_jira_epics(dryrun=False, diagnose=False):
    """Assign children to their parent Epics in OpenProject."""
    print('\n' + '=' * 60)
    print('ASSIGN EPICS: Setting parent-child relationships')
    print('=' * 60)
    
    # Fetch Jira Epics
    jira_epics = fetch_jira_epics()
    
    # Fetch OpenProject work packages
    op_work_packages = fetch_op_work_packages()
    
    # Map Epics
    op_epic_mapping = build_op_mapping(op_work_packages, jira_epics, type_filter='Epic', diagnose=diagnose)
    
    if diagnose:
        print('\nDiagnose mode complete. No assignments made.')
        return
    
    # Process each Epic
    total_assignments = 0
    
    for epic_key, epic_summary in jira_epics.items():
        if epic_key not in op_epic_mapping:
            print(f'\nSkipping Epic {epic_key}: No mapping in OpenProject.')
            continue
        
        op_epic_id = op_epic_mapping[epic_key]
        
        # Fetch children for this Epic
        jira_children = fetch_jira_children(epic_key)
        
        if not jira_children:
            continue
        
        # Map children to OpenProject
        op_child_mapping = build_op_mapping(op_work_packages, jira_children)
        
        # Assign children to Epic
        for jira_key, op_child_id in op_child_mapping.items():
            if set_work_package_parent(op_child_id, op_epic_id, dryrun):
                total_assignments += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('ASSIGN EPICS SUMMARY')
    print('=' * 60)
    print(f'Total Jira Epics: {len(jira_epics)}')
    print(f'Mapped to OpenProject: {len(op_epic_mapping)}')
    print(f'Child assignments: {total_assignments}')


def list_jira_epics():
    """List all Epics in both Jira and OpenProject."""
    print('\n' + '=' * 60)
    print('LISTING EPICS')
    print('=' * 60)
    
    # Jira Epics
    print('\n--- Jira Epics ---')
    jira_epics = fetch_jira_epics()
    for key, summary in jira_epics.items():
        print(f'  {key}: {summary}')
    
    # OpenProject Epics
    print('\n--- OpenProject Epics ---')
    op_work_packages = fetch_op_work_packages()
    types = fetch_op_types()
    epic_type_id = types.get('Epic')
    
    op_epics = []
    for wp in op_work_packages:
        type_href = wp['_links']['type']['href']
        type_id = int(type_href.split('/')[-1]) if type_href else None
        if type_id == epic_type_id:
            op_epics.append(wp)
    
    for wp in op_epics:
        jira_id = wp.get(f'customField{JIRA_ID_CUSTOM_FIELD}', 'N/A')
        print(f'  OP {wp["id"]} (Jira: {jira_id}): {wp["subject"]}')
    
    print(f'\nTotal Jira Epics: {len(jira_epics)}')
    print(f'Total OpenProject Epics: {len(op_epics)}')


# =============================================================================
# Migration Functions - Confluence to BookStack
# =============================================================================

def sync_confluence_pages(dryrun=False, skip_existing=True, space_key=None, book_id=None):
    """Sync pages from Confluence to BookStack."""
    print('\n' + '=' * 60)
    print('SYNC PAGES: Migrating from Confluence to BookStack')
    print('=' * 60)
    
    space_key = space_key or CONFLUENCE_SPACE_KEY
    book_id = book_id or BOOKSTACK_BOOK_ID
    
    if not space_key:
        print('Error: CONFLUENCE_SPACE_KEY must be specified')
        return
    
    if not book_id:
        print('Error: BOOKSTACK_BOOK_ID must be specified')
        return
    
    # Fetch Confluence pages
    print(f'Fetching pages from Confluence space: {space_key}')
    confluence_pages = fetch_confluence_pages(space_key=space_key)
    
    # Build cache of existing BookStack pages
    print('\nBuilding cache of existing BookStack pages...')
    bookstack_pages = fetch_bookstack_pages(book_id=book_id)
    existing_by_confluence_id = {}
    
    # Simple mapping - in production, you'd store Confluence ID in page metadata
    # For now, we'll match by title (name)
    for page in bookstack_pages:
        # You might want to store Confluence ID in the page HTML as a comment
        existing_by_confluence_id[page['name']] = page
    
    print(f'Found {len(existing_by_confluence_id)} existing pages in BookStack.')
    
    # Process pages
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    
    # Build a map of Confluence page IDs to pages for hierarchy
    confluence_pages_map = {page['id']: page for page in confluence_pages}
    parent_map = {}  # Map Confluence page ID to BookStack page ID
    
    # Sort pages by depth (root pages first)
    def get_depth(page):
        ancestors = page.get('ancestors', [])
        return len(ancestors)
    
    sorted_pages = sorted(confluence_pages, key=get_depth)
    
    for page in sorted_pages:
        page_id = page['id']
        page_title = page['title']
        
        print(f'\nProcessing {page_id}: {page_title[:50]}...')
        
        # Check if already exists (by name for now)
        existing_page = existing_by_confluence_id.get(page_title)
        
        if existing_page and skip_existing:
            print(f'  Skipping - already exists as page {existing_page["id"]}')
            skipped += 1
            parent_map[page_id] = existing_page['id']
            continue
        
        # Get page content (HTML)
        body = page.get('body', {})
        storage = body.get('storage', {})
        html_content = convert_atlassian_storage_to_html(storage)
        
        # Determine parent in BookStack
        parent_bookstack_id = None
        ancestors = page.get('ancestors', [])
        if ancestors:
            parent_confluence_id = ancestors[-1]['id']
            parent_bookstack_id = parent_map.get(parent_confluence_id)
        
        if dryrun:
            if existing_page:
                print(f'  [DRYRUN] Would update page {existing_page["id"]}')
                updated += 1
            else:
                print(f'  [DRYRUN] Would create new page (parent: {parent_bookstack_id})')
                created += 1
            if not existing_page:
                parent_map[page_id] = 'dryrun_id'
            continue
        
        try:
            if existing_page:
                # Update existing
                result = update_bookstack_page(existing_page['id'], name=page_title, html=html_content, dryrun=dryrun)
                if result:
                    print(f'  Updated page {existing_page["id"]}')
                    updated += 1
                    parent_map[page_id] = existing_page['id']
                else:
                    errors += 1
            else:
                # Create new
                result = create_bookstack_page(
                    name=page_title,
                    html=html_content,
                    book_id=book_id,
                    parent_id=parent_bookstack_id,
                    dryrun=dryrun
                )
                if result:
                    print(f'  Created page {result["id"]}')
                    created += 1
                    parent_map[page_id] = result['id']
                    existing_by_confluence_id[page_title] = result
                else:
                    errors += 1
        except Exception as e:
            print(f'  Error: {e}')
            errors += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('SYNC PAGES SUMMARY')
    print('=' * 60)
    print(f'Total processed: {len(confluence_pages)}')
    print(f'Created: {created}')
    print(f'Updated: {updated}')
    print(f'Skipped: {skipped}')
    print(f'Errors: {errors}')


def sync_confluence_spaces(dryrun=False, skip_existing=True):
    """Sync spaces from Confluence to BookStack (as books)."""
    print('\n' + '=' * 60)
    print('SYNC SPACES: Migrating Confluence spaces to BookStack books')
    print('=' * 60)
    
    # Fetch Confluence spaces
    confluence_spaces = fetch_confluence_spaces()
    
    # Build cache of existing BookStack books
    print('\nBuilding cache of existing BookStack books...')
    bookstack_books = fetch_bookstack_books()
    existing_by_name = {book['name']: book for book in bookstack_books}
    print(f'Found {len(existing_by_name)} existing books in BookStack.')
    
    # Process spaces
    created = 0
    skipped = 0
    errors = 0
    
    for space in confluence_spaces:
        space_key = space['key']
        space_name = space['name']
        space_description = space.get('description', {}).get('plain', {}).get('value', '')
        
        print(f'\nProcessing space {space_key}: {space_name}...')
        
        # Check if already exists
        existing_book = existing_by_name.get(space_name)
        
        if existing_book and skip_existing:
            print(f'  Skipping - already exists as book {existing_book["id"]}')
            skipped += 1
            continue
        
        if dryrun:
            if existing_book:
                print(f'  [DRYRUN] Would update book {existing_book["id"]}')
            else:
                print(f'  [DRYRUN] Would create new book: {space_name}')
            created += 1
            continue
        
        try:
            result = create_bookstack_book(
                name=space_name,
                description=space_description,
                dryrun=dryrun
            )
            if result:
                print(f'  Created book {result["id"]}')
                created += 1
                existing_by_name[space_name] = result
            else:
                errors += 1
        except Exception as e:
            print(f'  Error: {e}')
            errors += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('SYNC SPACES SUMMARY')
    print('=' * 60)
    print(f'Total processed: {len(confluence_spaces)}')
    print(f'Created: {created}')
    print(f'Skipped: {skipped}')
    print(f'Errors: {errors}')


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Migration Tool - Jira to OpenProject / Confluence to BookStack',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Jira to OpenProject
  python migrate.py jira --sync-issues --dryrun
  python migrate.py jira --sync-issues
  python migrate.py jira --assign-epics --dryrun
  python migrate.py jira --diagnose
  python migrate.py jira --list-epics
  
  # Confluence to BookStack
  python migrate.py confluence --sync-pages --dryrun
  python migrate.py confluence --sync-pages
  python migrate.py confluence --sync-spaces --dryrun
        """
    )
    
    # Migration type (source system)
    parser.add_argument('migration_type', choices=['jira', 'confluence'],
                        help='Source system type (jira or confluence)')
    
    # Mode selection (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    
    # Jira modes
    mode_group.add_argument('--sync-issues', action='store_true',
                            help='Sync issues from Jira to OpenProject')
    mode_group.add_argument('--assign-epics', action='store_true',
                            help='Assign children to their parent Epics (Jira only)')
    mode_group.add_argument('--diagnose', action='store_true',
                            help='Show diagnostics for unmatched Epics (Jira only)')
    mode_group.add_argument('--list-epics', action='store_true',
                            help='List all Epics in Jira and OpenProject (Jira only)')
    
    # Confluence modes
    mode_group.add_argument('--sync-pages', action='store_true',
                            help='Sync pages from Confluence to BookStack (Confluence only)')
    mode_group.add_argument('--sync-spaces', action='store_true',
                            help='Sync spaces from Confluence to BookStack as books (Confluence only)')
    
    # Options
    parser.add_argument('--dryrun', action='store_true',
                        help='Preview changes without making them')
    parser.add_argument('--update-existing', action='store_true',
                        help='Update existing items (default: skip)')
    parser.add_argument('--issues', type=str,
                        help='Comma-separated list of specific Jira issue keys to process')
    
    args = parser.parse_args()
    
    migration_type = args.migration_type
    
    # Validate configuration based on migration type
    if migration_type == 'jira':
        if not all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]):
            print('Error: Jira credentials not configured in .env')
            print('Required: JIRA_HOST, JIRA_EMAIL, JIRA_API_TOKEN')
            return 1
        
        if not all([OP_BASE_URL, OP_API_KEY]):
            print('Error: OpenProject credentials not configured in .env')
            print('Required: OPENPROJECT_HOST, OPENPROJECT_API_KEY')
            return 1
        
        print(f'Jira Project: {JIRA_PROJECT_KEY}')
        print(f'OpenProject Project ID: {OP_PROJECT_ID}')
        print(f'Jira ID Custom Field: customField{JIRA_ID_CUSTOM_FIELD}')
    
    elif migration_type == 'confluence':
        if not all([CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN]):
            print('Error: Confluence credentials not configured in .env')
            print('Required: CONFLUENCE_HOST, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN')
            return 1
        
        if not all([BOOKSTACK_BASE_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET]):
            print('Error: BookStack credentials not configured in .env')
            print('Required: BOOKSTACK_HOST, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET')
            return 1
        
        print(f'Confluence Space Key: {CONFLUENCE_SPACE_KEY or "Not set"}')
        print(f'BookStack Book ID: {BOOKSTACK_BOOK_ID or "Not set"}')
    
    if args.dryrun:
        print('\n*** DRY RUN MODE - No changes will be made ***\n')
    
    # Execute selected mode
    if migration_type == 'jira':
        if args.sync_issues:
            specific_keys = args.issues.split(',') if args.issues else None
            sync_jira_issues(
                dryrun=args.dryrun,
                skip_existing=not args.update_existing,
                specific_keys=specific_keys
            )
        elif args.assign_epics:
            assign_jira_epics(dryrun=args.dryrun)
        elif args.diagnose:
            assign_jira_epics(diagnose=True)
        elif args.list_epics:
            list_jira_epics()
    
    elif migration_type == 'confluence':
        if args.sync_pages:
            sync_confluence_pages(
                dryrun=args.dryrun,
                skip_existing=not args.update_existing,
                space_key=CONFLUENCE_SPACE_KEY,
                book_id=int(BOOKSTACK_BOOK_ID) if BOOKSTACK_BOOK_ID else None
            )
        elif args.sync_spaces:
            sync_confluence_spaces(
                dryrun=args.dryrun,
                skip_existing=not args.update_existing
            )
    
    return 0


if __name__ == '__main__':
    exit(main())
