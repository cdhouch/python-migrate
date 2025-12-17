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
import time
from fuzzywuzzy import fuzz
from enum import Enum

# Optional HTML→Markdown conversion for pages with code blocks/tables
try:
    from markdownify import markdownify as _markdownify
except ImportError:
    _markdownify = None

try:
    from bs4 import BeautifulSoup
    _beautifulsoup_available = True
except ImportError:
    BeautifulSoup = None
    _beautifulsoup_available = False

if _markdownify is None:
    # This warning is helpful for Confluence → BookStack migrations where we
    # want to emit markdown rather than HTML so that code blocks render well.
    print('Warning: python package "markdownify" not installed; '
          'Confluence pages will be sent to BookStack as HTML only.')

load_dotenv()

# =============================================================================
# Adaptive Rate Limiting
# =============================================================================

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

def reset_rate_limit():
    """Reset rate limit delay back to minimum (call after successful batch)."""
    global _rate_limit_delay
    _rate_limit_delay = 0.1

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
BOOKSTACK_BOOK_ID = os.getenv('BOOKSTACK_BOOK_ID')  # Optional: specific book ID (legacy, for direct book migration)
BOOKSTACK_SHELF_ID = os.getenv('BOOKSTACK_SHELF_ID')  # Optional: specific shelf ID (for space migration)

# Authentication
confluence_auth = (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)
# BookStack uses header-based token auth: Authorization: Token {token_id}:{token_secret}
bookstack_headers = {
    'Authorization': f'Token {BOOKSTACK_TOKEN_ID}:{BOOKSTACK_TOKEN_SECRET}'
} if BOOKSTACK_TOKEN_ID and BOOKSTACK_TOKEN_SECRET else {}

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


def fetch_op_users():
    """Fetch all users from OpenProject."""
    # Use the same auth method as other OpenProject API calls
    url = f'{OP_BASE_URL}/api/v3/users'
    users = []
    offset = 1  # OpenProject uses 1-based offset
    page_size = 100
    
    while True:
        params = {'offset': offset, 'pageSize': page_size}
        response = requests.get(url, auth=op_auth, params=params)
        
        if response.status_code == 401:
            print(f'Error: Authentication failed for OpenProject users endpoint')
            print(f'Response: {response.text[:500]}')
            print(f'Using auth method: Basic auth with apikey:{OP_API_KEY[:10]}...')
            response.raise_for_status()
        
        response.raise_for_status()
        data = response.json()
        
        elements = data.get('_embedded', {}).get('elements', [])
        users.extend(elements)
        
        # Check if there are more pages
        total = data.get('total', 0)
        count = data.get('count', len(elements))
        
        if len(elements) < page_size or (total > 0 and offset + count - 1 >= total):
            break
        
        offset += page_size
        print(f'Fetched {len(users)} users so far...')
    
    print(f'Found {len(users)} users in OpenProject.')
    return users


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
        url = f'https://{CONFLUENCE_BASE_URL}/wiki/rest/api/space/{space_key}'
        response = requests.get(url, auth=confluence_auth)
        response.raise_for_status()
        return [response.json()]
    else:
        url = f'https://{CONFLUENCE_BASE_URL}/wiki/rest/api/space'
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


def fetch_confluence_pages(space_key=None, page_id=None, expand='body.storage,version,ancestors,space,history'):
    """Fetch pages and folders from Confluence. Can fetch all content in a space or a specific item."""
    if page_id:
        url = f'https://{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}'
        params = {'expand': expand}
        response = requests.get(url, auth=confluence_auth, params=params)
        response.raise_for_status()
        return [response.json()]
    
    url = f'https://{CONFLUENCE_BASE_URL}/wiki/rest/api/content'
    all_content = []
    start = 0
    limit = 50
    
    # Fetch both pages and folders (folders may not be supported in all Confluence versions)
    content_types = ['page', 'folder']
    
    for content_type in content_types:
        start = 0
        params = {
            'start': start,
            'limit': limit,
            'expand': expand,
            'type': content_type
        }
        
        if space_key:
            params['spaceKey'] = space_key
        
        try:
            while True:
                params['start'] = start
                response = requests.get(url, auth=confluence_auth, params=params)
                response.raise_for_status()
                data = response.json()
                
                all_content.extend(data.get('results', []))
                
                if len(data.get('results', [])) < limit:
                    break
                start += limit
                
                print(f'Fetched {len(all_content)} items so far...')
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 501:
                # Folders not supported in this Confluence instance, skip
                print(f'Note: Folders are not supported in this Confluence instance (type={content_type})')
                continue
            else:
                raise
    
    print(f'Found {len(all_content)} items (pages and folders) in Confluence.')
    return all_content


def fetch_confluence_page_children(page_id, expand='body.storage,version'):
    """Fetch child pages of a specific Confluence page."""
    url = f'https://{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}/child/page'
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


def fetch_atlassian_users():
    """Fetch all users from Atlassian using Jira API (Jira/Confluence share the same user directory)."""
    # Use Jira API to fetch users (works for both Jira and Confluence)
    users = []
    start_at = 0
    max_results = 50
    
    url = f'https://{JIRA_BASE_URL}/rest/api/3/users/search'
    
    while True:
        # Jira user search API - include email addresses if we have permission
        params = {
            'startAt': start_at,
            'maxResults': max_results
        }
        
        try:
            response = requests.get(url, auth=jira_auth, params=params)
            response.raise_for_status()
            batch_users = response.json()
            
            if not batch_users:
                break
            
            users.extend(batch_users)
            
            # If we got fewer than max_results, we've reached the end
            if len(batch_users) < max_results:
                break
            
            start_at += max_results
            print(f'Fetched {len(users)} users so far...')
            
        except requests.exceptions.HTTPError as e:
            print(f'Error fetching Atlassian users: {e}')
            if response.status_code == 403:
                print('  Note: You may need admin permissions to list all users')
            break
        except Exception as e:
            print(f'Error: {e}')
            break
    
    print(f'Found {len(users)} users from search. Fetching detailed info with emails...')
    
    # The /users/search endpoint may not return email addresses due to privacy settings
    # Fetch individual user details which should include email addresses
    detailed_users = []
    for i, user_summary in enumerate(users, 1):
        account_id = user_summary.get('accountId')
        if account_id:
            # Fetch detailed user info which should include email
            user_url = f'https://{JIRA_BASE_URL}/rest/api/3/user'
            user_params = {'accountId': account_id}
            
            try:
                user_response = requests.get(user_url, auth=jira_auth, params=user_params)
                if user_response.status_code == 200:
                    user_detail = user_response.json()
                    detailed_users.append(user_detail)
                else:
                    # Fallback to summary if detail fetch fails
                    detailed_users.append(user_summary)
            except Exception:
                # Fallback to summary on error
                detailed_users.append(user_summary)
            
            # Rate limiting - small delay
            if i % 20 == 0:
                time.sleep(0.5)
                print(f'  Fetched details for {i}/{len(users)} users...')
            else:
                time.sleep(0.05)
        else:
            detailed_users.append(user_summary)
    
    print(f'Retrieved detailed info for {len(detailed_users)} users.')
    return detailed_users


def _convert_confluence_macros_to_html(html):
    """
    Convert Confluence-specific macros (especially code macros) into plain HTML
    that BookStack understands (e.g., <pre><code> blocks).
    """
    if not html:
        return html
    if not ('ac:structured-macro' in html and 'ac:name="code"' in html):
        return html
    if not (_beautifulsoup_available if 'BeautifulSoup' in globals() else False):
        return html
    
    try:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Find all Confluence code macros
        def is_code_macro(tag):
            try:
                return (
                    isinstance(tag.name, str)
                    and tag.name.lower() == 'ac:structured-macro'
                    and tag.get('ac:name') == 'code'
                )
            except Exception:
                return False
        
        for macro in list(soup.find_all(is_code_macro)):
            # Default language is empty; we prefer bash if explicitly set
            language = ''
            for param in macro.find_all('ac:parameter'):
                if param.get('ac:name') == 'language':
                    language = (param.get_text() or '').strip()
                    break
            
            body = macro.find('ac:plain-text-body')
            if not body:
                continue
            code_text = body.get_text() or ''
            # Confluence stores literal "\n" sequences in CDATA; turn into newlines
            code_text = code_text.replace('\\n', '\n')
            
            # Build <pre><code> block
            pre = soup.new_tag('pre')
            code = soup.new_tag('code')
            if language:
                code['class'] = [f'language-{language}']
            code.string = code_text
            pre.append(code)
            
            macro.replace_with(pre)
        
        return str(soup)
    except Exception as e:
        print(f'  Warning: failed to convert Confluence macros to HTML: {e}')
        return html


def convert_atlassian_storage_to_html(storage):
    """Convert Atlassian Document Format (storage format) to HTML."""
    if not storage:
        return ''
    if isinstance(storage, str):
        return _convert_confluence_macros_to_html(storage)
    
    # If it's already HTML-like, normalize macros and return as-is
    if isinstance(storage, dict) and 'value' in storage:
        raw = storage.get('value', '')
        return _convert_confluence_macros_to_html(raw)
    
    return _convert_confluence_macros_to_html(str(storage))




# =============================================================================
# BookStack API Functions
# =============================================================================

def fetch_bookstack_shelves():
    """Fetch all shelves from BookStack."""
    global _bookstack_shelves_cache
    if _bookstack_shelves_cache:
        return _bookstack_shelves_cache
    
    url = f'{BOOKSTACK_BASE_URL}/api/shelves'
    response = requests.get(url, headers=bookstack_headers)
    response.raise_for_status()
    shelves = response.json().get('data', [])
    _bookstack_shelves_cache = {s['name']: s for s in shelves}
    print(f'Loaded {len(_bookstack_shelves_cache)} shelves from BookStack.')
    return _bookstack_shelves_cache


def create_bookstack_shelf(name, description='', dryrun=False):
    """Create a new shelf in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would create shelf: {name}')
        return {'id': 'dryrun_shelf_id', 'name': name}
    
    url = f'{BOOKSTACK_BASE_URL}/api/shelves'
    payload = {
        'name': name,
        'description': description or '',
    }
    
    response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        result = response.json()
        # Update cache
        global _bookstack_shelves_cache
        if _bookstack_shelves_cache is None:
            _bookstack_shelves_cache = {}
        _bookstack_shelves_cache[result['name']] = result
        return result
    else:
        print(f'Error creating shelf: {response.status_code}')
        print(response.text)
        return None


def fetch_bookstack_books(book_id=None):
    """Fetch books from BookStack. If book_id is provided, fetch only that book."""
    global _bookstack_books_cache
    
    if book_id:
        url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
        response = requests.get(url, headers=bookstack_headers)
        response.raise_for_status()
        return [response.json()]
    
    url = f'{BOOKSTACK_BASE_URL}/api/books'
    books = []
    page = 1
    per_page = 100
    
    while True:
        params = {'count': per_page, 'page': page}
        response = requests.get(url, headers=bookstack_headers, params=params)
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
    
    # Convert book_id to int for comparison
    book_id_int = int(book_id) if book_id else None
    api_total = None  # Will be set from first API response
    
    while True:
        params = {'count': per_page, 'page': page}
        # Use book_id in API call - the API filter works correctly
        if book_id:
            params['book_id'] = book_id  # Use original (string) value, API accepts it
        
        response = requests.get(url, headers=bookstack_headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        # Get total from API response (first time only)
        if api_total is None:
            api_total = data.get('total', 0)
            if api_total > 0:
                print(f'API reports {api_total} total pages for book {book_id}')
        
        page_batch = data.get('data', [])
        
        # Add all pages from this batch (API already filtered by book_id)
        pages.extend(page_batch)
        
        # Stop if we got fewer results than requested (end of pages)
        if len(page_batch) < per_page:
            break
        
        # Also stop if we've fetched at least as many as the API says exist
        if api_total and len(pages) >= api_total:
            break
        
        page += 1
        
        # Small delay to avoid rate limiting (adaptive)
        adaptive_sleep()
        if book_id_int:
            print(f'Fetched {len(pages)} pages from book {book_id_int} so far...')
        else:
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
    
    response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        return response.json()
    else:
        print(f'Error creating book: {response.status_code}')
        print(response.text)
        return None


def fetch_bookstack_users():
    """Fetch all users from BookStack."""
    url = f'{BOOKSTACK_BASE_URL}/api/users'
    users = []
    page = 1
    per_page = 100
    
    while True:
        params = {'count': per_page, 'page': page}
        response = requests.get(url, headers=bookstack_headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        user_batch = data.get('data', [])
        users.extend(user_batch)
        
        if len(user_batch) < per_page:
            break
        page += 1
    
    print(f'Found {len(users)} users in BookStack.')
    return users


def fetch_bookstack_chapters(book_id):
    """Fetch all chapters from a BookStack book."""
    url = f'{BOOKSTACK_BASE_URL}/api/chapters'
    chapters = []
    page = 1
    per_page = 100
    
    book_id_int = int(book_id) if book_id else None
    api_total = None
    
    while True:
        params = {'count': per_page, 'page': page, 'book_id': book_id}
        response = requests.get(url, headers=bookstack_headers, params=params)
        
        # Handle rate limiting
        if response.status_code == 429:
            handle_rate_limit()
            continue
        
        response.raise_for_status()
        data = response.json()
        
        # Get total from API response (first time only)
        if api_total is None:
            api_total = data.get('total', 0)
            if api_total > 0:
                print(f'API reports {api_total} total chapters for book {book_id}')
        
        chapter_batch = data.get('data', [])
        
        # Filter by book_id if needed (API should filter but verify)
        if book_id_int:
            chapter_batch = [c for c in chapter_batch if c.get('book_id') == book_id_int]
        
        chapters.extend(chapter_batch)
        
        # Stop if we've fetched all chapters according to API total
        if api_total and len(chapters) >= api_total:
            break
        
        # Also stop if we got fewer results than requested (end of chapters)
        if len(chapter_batch) < per_page:
            break
        
        page += 1
        
        # Rate limiting - small delay between requests (adaptive)
        adaptive_sleep()
    
    return chapters


def create_bookstack_chapter(name, description, book_id, dryrun=False):
    """Create a new chapter in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would create chapter: {name} in book {book_id}')
        return {'id': 'dryrun_chapter_id', 'name': name}
    
    url = f'{BOOKSTACK_BASE_URL}/api/chapters'
    payload = {
        'name': name,
        'description': description or '',
        'book_id': book_id,
    }
    
    response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        return response.json()
    else:
        print(f'Error creating chapter: {response.status_code}')
        print(response.text)
        return None


def create_bookstack_user(name, email, password=None, dryrun=False):
    """Create a new user in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would create user: {name} ({email})')
        return {'id': 'dryrun_user_id', 'name': name, 'email': email}
    
    url = f'{BOOKSTACK_BASE_URL}/api/users'
    payload = {
        'name': name,
        'email': email,
    }
    
    # Password is optional - if not provided, user will need to reset password
    if password:
        payload['password'] = password
    
    response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        return response.json()
    else:
        print(f'Error creating user: {response.status_code}')
        print(response.text)
        return None


def create_bookstack_page(name, html=None, book_id=None, chapter_id=None, parent_id=None, owner_id=None, dryrun=False):
    """Create a new page in BookStack.
    
    The ``html`` parameter should contain the page content as HTML.
    """
    if dryrun:
        print(f'[DRYRUN] Would create page: {name} in book {book_id}')
        return {'id': 'dryrun_id', 'name': name}
    
    url = f'{BOOKSTACK_BASE_URL}/api/pages'
    payload = {
        'name': name,
        'book_id': book_id,
        'html': html or '',
    }
    
    if chapter_id:
        payload['chapter_id'] = chapter_id
    if parent_id:
        payload['parent_id'] = parent_id
    if owner_id:
        payload['owned_by'] = owner_id
    
    response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.post(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        result = response.json()
        # Debug: check if chapter was set
        if chapter_id and result.get('chapter_id') != chapter_id:
            print(f'  Warning: Requested chapter_id {chapter_id} but page has chapter_id {result.get("chapter_id")}')
        # Debug: check if owner was set
        if owner_id and result.get('owned_by') != owner_id:
            print(f'  Warning: Requested owner_id {owner_id} but page has owner_id {result.get("owned_by")}')
        return result
    else:
        print(f'Error creating page: {response.status_code}')
        print(response.text)
        return None


def update_bookstack_page(page_id, name=None, html=None, parent_id=None, chapter_id=None, owner_id=None, dryrun=False):
    """Update an existing page in BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would update page {page_id}')
        return True
    
    url = f'{BOOKSTACK_BASE_URL}/api/pages/{page_id}'
    
    # Get current page data
    response = requests.get(url, headers=bookstack_headers)
    if response.status_code != 200:
        print(f'Error fetching page {page_id}: {response.status_code}')
        return False
    
    current_data = response.json()
    payload = {}
    
    if name is not None:
        payload['name'] = name
    if html is not None:
        payload['html'] = html
    if parent_id is not None:
        payload['parent_id'] = parent_id
    if chapter_id is not None:
        payload['chapter_id'] = chapter_id
    if owner_id is not None:
        payload['owned_by'] = owner_id
    
    if not payload:
        return True
    
    # Include required fields
    payload['book_id'] = current_data.get('book_id')
    
    response = requests.put(url, headers=bookstack_headers, json=payload)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.put(url, headers=bookstack_headers, json=payload)
    if response.status_code in [200, 201]:
        # Verify the update actually worked
        if owner_id is not None:
            updated_data = response.json()
            if updated_data.get('owned_by') != owner_id:
                print(f'  Warning: Update requested owner_id {owner_id} but page still has owner_id {updated_data.get("owned_by")}')
        return True
    else:
        print(f'Error updating page {page_id}: {response.status_code}')
        print(response.text)
        return False


def delete_bookstack_chapter(chapter_id, dryrun=False):
    """Delete a chapter from BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would delete chapter {chapter_id}')
        return True
    
    url = f'{BOOKSTACK_BASE_URL}/api/chapters/{chapter_id}'
    response = requests.delete(url, headers=bookstack_headers)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.delete(url, headers=bookstack_headers)
    if response.status_code in [200, 204]:
        return True
    else:
        print(f'Error deleting chapter {chapter_id}: {response.status_code}')
        print(response.text)
        return False


def delete_bookstack_page(page_id, dryrun=False):
    """Delete a page from BookStack."""
    if dryrun:
        print(f'[DRYRUN] Would delete page {page_id}')
        return True
    
    url = f'{BOOKSTACK_BASE_URL}/api/pages/{page_id}'
    response = requests.delete(url, headers=bookstack_headers)
    if response.status_code == 429:
        handle_rate_limit()
        # Retry once
        response = requests.delete(url, headers=bookstack_headers)
    if response.status_code in [200, 204]:
        return True
    else:
        print(f'Error deleting page {page_id}: {response.status_code}')
        print(response.text)
        return False


def delete_all_bookstack_pages(book_id=None, dryrun=False):
    """Delete all pages and chapters from a BookStack book."""
    
    print('\n' + '=' * 60)
    print('DELETE CONTENT: Removing all pages and chapters from BookStack book')
    print('=' * 60)
    
    book_id = book_id or BOOKSTACK_BOOK_ID
    
    if not book_id:
        print('Error: BOOKSTACK_BOOK_ID must be specified')
        return
    
    if dryrun:
        print('\n[DRYRUN] Would fetch and delete all chapters and pages')
        return
    
    # Keep deleting until nothing remains
    max_iterations = 10  # Safety limit
    iteration = 0
    
    while iteration < max_iterations:
        iteration += 1
        print(f'\n--- Iteration {iteration} ---')
        
        # Fetch pages
        print(f'Fetching pages from book {book_id}...')
        pages = fetch_bookstack_pages(book_id=book_id)
        
        # Deduplicate pages by ID
        seen_page_ids = set()
        unique_pages = []
        for page in pages:
            page_id = page.get('id')
            if page_id and page_id not in seen_page_ids:
                seen_page_ids.add(page_id)
                unique_pages.append(page)
        
        pages = unique_pages
        print(f'Found {len(pages)} pages to delete.')
        
        # Delete pages
        pages_deleted = 0
        pages_errors = 0
        
        if pages:
            pages_sorted = sorted(pages, key=lambda p: p.get('id', 0), reverse=True)
            
            for i, page in enumerate(pages_sorted, 1):
                page_id = page['id']
                page_name = page['name']
                # Show progress every 10 pages or for every page if less than 50 total
                if len(pages_sorted) <= 50 or i % 10 == 0 or i == len(pages_sorted):
                    print(f'[{i}/{len(pages_sorted)}] Deleting page {page_id}: {page_name[:50]}...')
                
                if delete_bookstack_page(page_id, dryrun=False):
                    pages_deleted += 1
                else:
                    pages_errors += 1
                
                # Rate limiting (adaptive)
                adaptive_sleep()
        
        # Fetch chapters
        print(f'\nFetching chapters from book {book_id}...')
        chapters = fetch_bookstack_chapters(book_id)
        
        # Deduplicate chapters by ID
        seen_chapter_ids = set()
        unique_chapters = []
        for chapter in chapters:
            chapter_id = chapter.get('id')
            if chapter_id and chapter_id not in seen_chapter_ids:
                seen_chapter_ids.add(chapter_id)
                unique_chapters.append(chapter)
        
        chapters = unique_chapters
        print(f'Found {len(chapters)} chapters to delete.')
        
        # Delete chapters
        chapters_deleted = 0
        chapters_errors = 0
        
        if chapters:
            chapters_sorted = sorted(chapters, key=lambda c: c.get('id', 0), reverse=True)
            
            for i, chapter in enumerate(chapters_sorted, 1):
                chapter_id = chapter['id']
                chapter_name = chapter['name']
                # Show progress every 10 chapters or for every chapter if less than 50 total
                if len(chapters_sorted) <= 50 or i % 10 == 0 or i == len(chapters_sorted):
                    print(f'[{i}/{len(chapters_sorted)}] Deleting chapter {chapter_id}: {chapter_name[:50]}...')
                
                if delete_bookstack_chapter(chapter_id, dryrun=False):
                    chapters_deleted += 1
                else:
                    chapters_errors += 1
                
                # Rate limiting (adaptive)
                adaptive_sleep()
        
        # If nothing was found or deleted, we're done
        if len(pages) == 0 and len(chapters) == 0:
            print('\nNo more pages or chapters found. Deletion complete!')
            break
        
        print(f'\nIteration {iteration} summary: Deleted {pages_deleted} pages, {chapters_deleted} chapters')
        
        # Small delay between iterations (adaptive)
        adaptive_sleep()
    
    print('\n' + '=' * 60)
    print('DELETE CONTENT COMPLETE')
    print('=' * 60)
    print(f'Completed {iteration} iteration(s)')


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
# Migration Functions - User Management
# =============================================================================

def load_user_email_mapping():
    """Load user email mapping from user_map.json file."""
    mapping = {}
    user_map_file = os.path.join(os.path.dirname(__file__), 'user_map.json')
    
    if os.path.exists(user_map_file):
        try:
            with open(user_map_file, 'r') as f:
                mapping = json.load(f)
            print(f'Loaded {len(mapping)} user email mappings from user_map.json')
        except Exception as e:
            print(f'Warning: Could not load user_map.json: {e}')
            print('  User email mapping will not be available.')
    else:
        print(f'Note: user_map.json not found. User email mapping will not be available.')
        print(f'  Create user_map.json (see user_map.json.example) to map display names to emails.')
    
    return mapping


def sync_users_to_bookstack(source='atlassian', dryrun=False, skip_existing=True):
    """
    Sync users from a source (atlassian or openproject) to BookStack.
    
    Args:
        source: 'atlassian' or 'openproject'
        dryrun: If True, don't actually create users
        skip_existing: If True, skip users that already exist in BookStack (by email)
    """
    print('\n' + '=' * 60)
    print(f'SYNC USERS: Migrating users from {source.upper()} to BookStack')
    print('=' * 60)
    
    # Load user email mapping if available
    user_email_mapping = load_user_email_mapping()
    
    # Fetch users from source
    if source == 'atlassian':
        source_users = fetch_atlassian_users()
    elif source == 'openproject':
        source_users = fetch_op_users()
    else:
        print(f'Error: Unknown source "{source}". Use "atlassian" or "openproject".')
        return
    
    if not source_users:
        print('No users found in source system.')
        return
    
    # Fetch existing BookStack users
    print('\nBuilding cache of existing BookStack users...')
    bookstack_users = fetch_bookstack_users()
    existing_by_email = {user.get('email', '').lower(): user for user in bookstack_users if user.get('email')}
    
    print(f'Found {len(existing_by_email)} existing users in BookStack.')
    
    # Process users
    created = 0
    skipped = 0
    errors = 0
    
    for user in source_users:
        if source == 'atlassian':
            # Atlassian user format
            user_key = user.get('userKey', user.get('accountId', ''))
            display_name = user.get('displayName', user.get('name', 'Unknown'))
            email = user.get('emailAddress', '')
            if not email:
                # Try alternative field names
                email = user.get('email', '')
            if not email:
                # Use mapping from user_map.json if available
                email = user_email_mapping.get(display_name, '')
        elif source == 'openproject':
            # OpenProject user format
            user_id = user.get('id')
            display_name = user.get('name', 'Unknown')
            email = user.get('email', '')
            if not email:
                # Try login field
                email = user.get('login', '')
        
        if not email:
            print(f'  Skipping user "{display_name}" - no email address')
            skipped += 1
            continue
        
        email_lower = email.lower()
        
        # Check if already exists
        if email_lower in existing_by_email and skip_existing:
            print(f'  Skipping {display_name} ({email}) - already exists')
            skipped += 1
            continue
        
        if dryrun:
            print(f'  [DRYRUN] Would create user: {display_name} ({email})')
            created += 1
        else:
            # Create user in BookStack (without password - user will need to reset)
            result = create_bookstack_user(
                name=display_name,
                email=email,
                password=None,  # Don't set password - user can reset via email
                dryrun=dryrun
            )
            if result:
                print(f'  Created user: {display_name} ({email})')
                created += 1
                existing_by_email[email_lower] = result
                adaptive_sleep()  # Adaptive rate limit protection
            else:
                errors += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('SYNC USERS SUMMARY')
    print('=' * 60)
    print(f'Total processed: {len(source_users)}')
    print(f'Created: {created}')
    print(f'Skipped: {skipped}')
    print(f'Errors: {errors}')
    
    return existing_by_email


# =============================================================================
# Migration Functions - Confluence to BookStack
# =============================================================================

def sync_confluence_pages(dryrun=False, skip_existing=True, space_key=None, shelf_id=None, book_id=None, user_map=None, page_id=None, page_title=None):
    """
    Sync pages from Confluence to BookStack.
    
    New hierarchy: Space -> Shelf, Top-level pages -> Books, Pages -> Chapters/Pages
    If shelf_id is provided, uses new hierarchy. If book_id is provided, uses legacy single-book mode.
    """
    print('\n' + '=' * 60)
    print('SYNC PAGES: Migrating from Confluence to BookStack')
    print('=' * 60)
    
    space_key = space_key or CONFLUENCE_SPACE_KEY
    shelf_id = shelf_id or BOOKSTACK_SHELF_ID
    book_id = book_id or BOOKSTACK_BOOK_ID
    
    if not space_key:
        print('Error: CONFLUENCE_SPACE_KEY must be specified')
        return
    
    # If shelf_id is not provided, automatically create a shelf from the Confluence space
    if not shelf_id and not book_id:
        print('No BOOKSTACK_SHELF_ID specified, creating shelf from Confluence space...')
        # Fetch the Confluence space to get its name
        confluence_spaces = fetch_confluence_spaces(space_key=space_key)
        if not confluence_spaces:
            print(f'Error: Space {space_key} not found in Confluence')
            return
        
        space = confluence_spaces[0]
        space_name = space['name']
        space_description = space.get('description', {}).get('plain', {}).get('value', '')
        
        # Check if shelf already exists
        bookstack_shelves = fetch_bookstack_shelves()
        existing_shelf = bookstack_shelves.get(space_name)
        
        if existing_shelf:
            shelf_id = existing_shelf['id']
            print(f'  Found existing shelf "{space_name}" (ID: {shelf_id}), using it')
        else:
            if dryrun:
                print(f'  [DRYRUN] Would create shelf "{space_name}"')
                shelf_id = 'dryrun_shelf_id'
            else:
                result = create_bookstack_shelf(
                    name=space_name,
                    description=space_description,
                    dryrun=dryrun
                )
                if result:
                    shelf_id = result['id']
                    print(f'  Created shelf "{space_name}" (ID: {shelf_id})')
                else:
                    print(f'  Error creating shelf "{space_name}"')
                    return
    
    # Determine which mode to use
    use_shelf_mode = shelf_id is not None and shelf_id != 'dryrun_shelf_id'
    use_legacy_mode = book_id is not None
    
    if not use_shelf_mode and not use_legacy_mode:
        if shelf_id == 'dryrun_shelf_id':
            use_shelf_mode = True  # Allow dryrun to proceed
        else:
            print('Error: Either BOOKSTACK_SHELF_ID or BOOKSTACK_BOOK_ID must be specified')
            print('  Use BOOKSTACK_SHELF_ID for new hierarchy (Space->Shelf->Books->Chapters->Pages)')
            print('  Use BOOKSTACK_BOOK_ID for legacy mode (Space->Book->Chapters->Pages)')
            return
    
    if use_shelf_mode:
        print(f'Using shelf-based hierarchy (shelf_id: {shelf_id})')
    else:
        print(f'Using legacy single-book mode (book_id: {book_id})')
    
    # Fetch Confluence pages
    # Fetch Confluence pages
    if page_id:
        print(f'Fetching single Confluence page by ID: {page_id}')
        confluence_pages = fetch_confluence_pages(page_id=page_id)
    else:
        print(f'Fetching pages from Confluence space: {space_key}')
        confluence_pages = fetch_confluence_pages(space_key=space_key)

    # Optional filter by title for faster iteration/testing
    if page_title:
        before_count = len(confluence_pages)
        confluence_pages = [p for p in confluence_pages if p.get('title') == page_title]
        print(f'Filtered pages by title "{page_title}": {len(confluence_pages)} of {before_count} remain')
        if not confluence_pages:
            print('Warning: No Confluence pages matched the requested title; nothing to sync.')
            return
    
    # Build user mapping if not provided
    if user_map is None:
        print('\nBuilding user mapping from BookStack users...')
        bookstack_users = fetch_bookstack_users()
        user_map = {user.get('email', '').lower(): user.get('id') for user in bookstack_users if user.get('email')}
        print(f'Found {len(user_map)} users in BookStack for assignment.')
    
    # Also load user email mapping for display name fallback
    user_email_mapping = load_user_email_mapping()
    # Create reverse mapping: email -> BookStack user ID
    email_to_user_id = {}
    for email, user_id in user_map.items():
        email_to_user_id[email] = user_id
    # Also add mappings from user_map.json
    for display_name, email in user_email_mapping.items():
        email_lower = email.lower()
        if email_lower in user_map:
            email_to_user_id[email_lower] = user_map[email_lower]
    
    # Build a map of Confluence page IDs to pages for hierarchy
    confluence_pages_map = {page['id']: page for page in confluence_pages}
    
    # Extract parent pages from ancestor data that might not be in the main pages list
    # These are often "folders" in Confluence that should become books
    print('\nExtracting parent pages from ancestor data (may include folders)...')
    parent_pages_from_ancestors = {}
    total_pages = len(confluence_pages)
    for idx, page in enumerate(confluence_pages, 1):
        if idx % 50 == 0 or idx == total_pages:
            print(f'  Processing page {idx}/{total_pages}...')
        ancestors = page.get('ancestors', [])
        for ancestor in ancestors:
            ancestor_id = ancestor.get('id')
            # If this ancestor isn't in our pages list, add it as a potential folder
            if ancestor_id and ancestor_id not in confluence_pages_map:
                if ancestor_id not in parent_pages_from_ancestors:
                    # Create a minimal page object from ancestor data
                    parent_pages_from_ancestors[ancestor_id] = {
                        'id': ancestor_id,
                        'title': ancestor.get('title', 'Unknown'),
                        'type': 'page',  # Assume it's a page, even if it acts as a folder
                        'ancestors': ancestors[:ancestors.index(ancestor)],  # Ancestors before this one
                        '_is_folder': True  # Mark as likely folder
                    }
    
    # Add these parent pages to our pages list and map
    if parent_pages_from_ancestors:
        print(f'Found {len(parent_pages_from_ancestors)} parent pages/folders referenced but not in main list:')
        for parent_id, parent_page in parent_pages_from_ancestors.items():
            print(f'  - {parent_page["title"]} (ID: {parent_id})')
            confluence_pages.append(parent_page)
            confluence_pages_map[parent_id] = parent_page
    
    # For shelf mode: Identify top-level pages (pages with no ancestors) - these become books
    top_level_pages = []
    page_to_book_map = {}  # Map Confluence page ID -> BookStack book ID
    
    if use_shelf_mode:
        print('\nIdentifying top-level pages (these will become books)...')
        # First, identify which pages have children (these are "folders" or container pages)
        print('  Checking which pages have children...')
        pages_with_children = set()
        total_pages = len(confluence_pages)
        for idx, page in enumerate(confluence_pages, 1):
            if idx % 50 == 0 or idx == total_pages:
                print(f'    Checking page {idx}/{total_pages}...')
            page_id = page['id']
            # Check if any other page has this page as an ancestor
            for other_page in confluence_pages:
                other_ancestors = other_page.get('ancestors', [])
                if other_ancestors and other_ancestors[-1]['id'] == page_id:
                    pages_with_children.add(page_id)
                    break
        
        # Pages with 0, 1, or 2 ancestors that have children become books
        # This makes the structure more granular: each major topic/folder becomes its own book
        # 0 ancestors = root pages (if they have children)
        # 1 ancestor = direct children of root (if they have children)
        # 2 ancestors = grandchildren of root (if they have children, like "Fox Resources", "Projects Folder")
        for page in confluence_pages:
            page_id = page['id']
            ancestors = page.get('ancestors', [])
            ancestor_count = len(ancestors)
            # Only create books for pages that have children (are folders/containers) and are at depth 0-2
            if ancestor_count <= 2 and page_id in pages_with_children:
                top_level_pages.append(page)
                if ancestor_count == 0:
                    print(f'  Root page (book): {page["title"]} (ID: {page["id"]})')
                elif ancestor_count == 1:
                    print(f'  Top-level child (book): {page["title"]} (ID: {page["id"]}, parent: {ancestors[0].get("title", "unknown")})')
                else:
                    print(f'  Second-level child (book): {page["title"]} (ID: {page["id"]}, grandparent: {ancestors[0].get("title", "unknown")}, parent: {ancestors[1].get("title", "unknown")})')
        
        print(f'\nFound {len(top_level_pages)} top-level pages. Creating books in shelf {shelf_id}...')
        
        # Fetch ALL existing books (not just in shelf) to check for duplicates
        all_existing_books = fetch_bookstack_books()
        # Create a map of book names to books (list of books with same name)
        existing_books_by_name = {}
        for book in all_existing_books:
            book_name = book['name']
            if book_name not in existing_books_by_name:
                existing_books_by_name[book_name] = []
            existing_books_by_name[book_name].append(book)
        
        # Merge duplicate books (books with same name)
        print('\nChecking for duplicate book names and merging...')
        books_to_delete = []
        for book_name, books_with_name in existing_books_by_name.items():
            if len(books_with_name) > 1:
                print(f'  Found {len(books_with_name)} books with name "{book_name}"')
                # Count pages in each book
                book_page_counts = []
                for book in books_with_name:
                    book_id = book['id']
                    pages = fetch_bookstack_pages(book_id=book_id)
                    page_count = len(pages)
                    chapters = fetch_bookstack_chapters(book_id)
                    chapter_count = len(chapters)
                    total_content = page_count + chapter_count
                    book_page_counts.append((book_id, total_content, page_count, chapter_count, book))
                    print(f'    Book ID {book_id}: {page_count} pages, {chapter_count} chapters (total: {total_content})')
                
                # Sort by total content count (descending), then by ID (ascending) for tie-breaking
                book_page_counts.sort(key=lambda x: (-x[1], x[0]))
                
                # Keep the book with most content (or first one if equal)
                keep_book = book_page_counts[0]
                keep_book_id = keep_book[0]
                keep_total = keep_book[1]
                print(f'    Keeping book ID {keep_book_id} ({keep_total} total items)')
                
                # Copy content from smaller books to the kept book, then delete smaller books
                for book_id, total_content, page_count, chapter_count, book in book_page_counts[1:]:
                    print(f'    Copying {page_count} pages and {chapter_count} chapters from book ID {book_id} into book ID {keep_book_id}...')
                    if not dryrun:
                        # Fetch all content from the book to merge
                        pages_to_copy = fetch_bookstack_pages(book_id=book_id)
                        chapters_to_copy = fetch_bookstack_chapters(book_id)
                        
                        # Create a map of old chapter IDs to new chapter IDs
                        chapter_id_map = {}
                        
                        # Copy chapters first (so we can map pages to new chapters)
                        for chapter in chapters_to_copy:
                            old_chapter_id = chapter['id']
                            chapter_name = chapter['name']
                            chapter_description = chapter.get('description', '')
                            # Create chapter in kept book
                            new_chapter = create_bookstack_chapter(
                                name=chapter_name,
                                description=chapter_description,
                                book_id=keep_book_id,
                                dryrun=dryrun
                            )
                            if new_chapter:
                                new_chapter_id = new_chapter['id']
                                chapter_id_map[old_chapter_id] = new_chapter_id
                                adaptive_sleep()
                        
                        # Copy pages to the kept book
                        copied_pages = 0
                        for page in pages_to_copy:
                            page_name = page['name']
                            page_html = page.get('html', '')
                            old_chapter_id = page.get('chapter_id')
                            new_chapter_id = chapter_id_map.get(old_chapter_id) if old_chapter_id else None
                            
                            # Create page in kept book
                            new_page = create_bookstack_page(
                                name=page_name,
                                html=page_html,
                                book_id=keep_book_id,
                                chapter_id=new_chapter_id,
                                dryrun=dryrun
                            )
                            if new_page:
                                copied_pages += 1
                                # Update chapter_id and owner if needed (BookStack API quirk)
                                if new_chapter_id:
                                    update_bookstack_page(new_page['id'], chapter_id=new_chapter_id, dryrun=dryrun)
                                adaptive_sleep()
                        
                        print(f'      Copied {len(chapter_id_map)} chapters and {copied_pages} pages')
                    
                    # Mark book for deletion
                    books_to_delete.append(book_id)
                
                # Update the map to only reference the kept book
                existing_books_by_name[book_name] = [keep_book[4]]
        
        # Delete merged books (after copying their content)
        if books_to_delete and not dryrun:
            print(f'\nDeleting {len(books_to_delete)} merged books (content has been copied to kept books)...')
            for book_id in books_to_delete:
                print(f'  Deleting book ID {book_id}...')
                url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
                response = requests.delete(url, headers=bookstack_headers)
                if response.status_code == 429:
                    handle_rate_limit()
                    response = requests.delete(url, headers=bookstack_headers)
                if response.status_code in [200, 204]:
                    print(f'    Successfully deleted book {book_id}')
                else:
                    print(f'    Warning: Could not delete book {book_id}: {response.status_code}')
                adaptive_sleep()
        
        # Convert back to single book per name (for compatibility with existing code)
        existing_books_by_name_single = {}
        for book_name, books_list in existing_books_by_name.items():
            existing_books_by_name_single[book_name] = books_list[0]
        existing_books_by_name = existing_books_by_name_single
        
        # Get current shelf data to track books in shelf
        # Handle dryrun shelf_id
        actual_shelf_id = shelf_id if shelf_id != 'dryrun_shelf_id' else None
        shelf_url = f'{BOOKSTACK_BASE_URL}/api/shelves/{actual_shelf_id}' if actual_shelf_id else None
        shelf_data = None
        existing_book_ids_in_shelf = []
        if not dryrun and actual_shelf_id:
            shelf_response = requests.get(shelf_url, headers=bookstack_headers)
            if shelf_response.status_code == 200:
                shelf_data = shelf_response.json()
                existing_book_ids_in_shelf = [b.get('id') for b in shelf_data.get('books', [])]
                print(f'  Found {len(existing_book_ids_in_shelf)} existing books in shelf')
            else:
                print(f'  Warning: Could not fetch shelf data: {shelf_response.status_code}')
                # Try to fetch shelf data again later if needed
        
        # After merging duplicates, ensure all kept books are in the shelf
        if not dryrun and actual_shelf_id:
            print('\nEnsuring all kept books (after merging) are in shelf...')
            for book_name, book in existing_books_by_name.items():
                kept_book_id = book['id']
                if kept_book_id not in existing_book_ids_in_shelf:
                    print(f'  Adding kept book "{book_name}" (ID: {kept_book_id}) to shelf')
                    existing_book_ids_in_shelf.append(kept_book_id)
        
        # Create a book for each top-level page
        for top_page in top_level_pages:
            page_id = top_page['id']
            page_title = top_page['title']
            
            # Check if book already exists (by name, regardless of shelf)
            existing_book = existing_books_by_name.get(page_title)
            if existing_book:
                book_id = existing_book['id']
                print(f'Book "{page_title}" already exists (ID: {book_id}), reusing it')
                page_to_book_map[page_id] = book_id
                # Add to shelf if not already there
                if book_id not in existing_book_ids_in_shelf:
                    existing_book_ids_in_shelf.append(book_id)
                continue
            
            if dryrun or not actual_shelf_id:
                print(f'[DRYRUN] Would create book "{page_title}" in shelf {shelf_id}')
                page_to_book_map[page_id] = 'dryrun_book_id'
            else:
                # Note: shelf_id is not passed here because BookStack API doesn't add books
                # to shelves during creation. We update the shelf after all books are created.
                result = create_bookstack_book(
                    name=page_title,
                    description='',  # Could extract from page content if needed
                    shelf_id=None,  # Shelf association handled separately
                    dryrun=dryrun
                )
                if result:
                    book_id = result['id']
                    print(f'Created book "{page_title}" (ID: {book_id})')
                    page_to_book_map[page_id] = book_id
                    # Update our cache to track newly created books
                    existing_books_by_name[page_title] = result
                    # Add to shelf list (will update shelf after all books are processed)
                    if book_id not in existing_book_ids_in_shelf:
                        existing_book_ids_in_shelf.append(book_id)
                    if not dryrun:
                        adaptive_sleep()  # Adaptive rate limit protection
                else:
                    print(f'  Error creating book for "{page_title}"')
        
        # After creating all books, check for and merge any duplicates that were just created
        if not dryrun:
            print('\nChecking for duplicate books created in this run...')
            # Re-fetch all books to get the current state
            all_books_after_creation = fetch_bookstack_books()
            books_by_name_after = {}
            for book in all_books_after_creation:
                book_name = book['name']
                if book_name not in books_by_name_after:
                    books_by_name_after[book_name] = []
                books_by_name_after[book_name].append(book)
            
            # Find duplicates
            duplicates_found = False
            books_to_delete_after_creation = []
            for book_name, books_with_name in books_by_name_after.items():
                if len(books_with_name) > 1:
                    duplicates_found = True
                    print(f'  Found {len(books_with_name)} books with name "{book_name}"')
                    # Count content in each book
                    book_content_counts = []
                    for book in books_with_name:
                        book_id = book['id']
                        pages = fetch_bookstack_pages(book_id=book_id)
                        chapters = fetch_bookstack_chapters(book_id)
                        total_content = len(pages) + len(chapters)
                        book_content_counts.append((book_id, total_content, len(pages), len(chapters), book))
                        print(f'    Book ID {book_id}: {len(pages)} pages, {len(chapters)} chapters (total: {total_content})')
                    
                    # Sort by total content (descending), then by ID (ascending) for tie-breaking
                    book_content_counts.sort(key=lambda x: (-x[1], x[0]))
                    
                    # Keep the book with most content
                    keep_book = book_content_counts[0]
                    keep_book_id = keep_book[0]
                    keep_total = keep_book[1]
                    print(f'    Keeping book ID {keep_book_id} ({keep_total} total items)')
                    
                    # Copy content from smaller books to the kept book, then delete smaller books
                    for book_id, total_content, page_count, chapter_count, book in book_content_counts[1:]:
                        print(f'    Copying {page_count} pages and {chapter_count} chapters from book ID {book_id} into book ID {keep_book_id}...')
                        # Fetch all content from the book to merge
                        pages_to_copy = fetch_bookstack_pages(book_id=book_id)
                        chapters_to_copy = fetch_bookstack_chapters(book_id)
                        
                        # Create a map of old chapter IDs to new chapter IDs
                        chapter_id_map = {}
                        
                        # Copy chapters first (so we can map pages to new chapters)
                        for chapter in chapters_to_copy:
                            old_chapter_id = chapter['id']
                            chapter_name = chapter['name']
                            chapter_description = chapter.get('description', '')
                            # Create chapter in kept book
                            new_chapter = create_bookstack_chapter(
                                name=chapter_name,
                                description=chapter_description,
                                book_id=keep_book_id,
                                dryrun=dryrun
                            )
                            if new_chapter:
                                new_chapter_id = new_chapter['id']
                                chapter_id_map[old_chapter_id] = new_chapter_id
                                adaptive_sleep()
                        
                        # Copy pages to the kept book
                        copied_pages = 0
                        for page in pages_to_copy:
                            page_name = page['name']
                            page_html = page.get('html', '')
                            old_chapter_id = page.get('chapter_id')
                            new_chapter_id = chapter_id_map.get(old_chapter_id) if old_chapter_id else None
                            
                            # Create page in kept book
                            new_page = create_bookstack_page(
                                name=page_name,
                                html=page_html,
                                book_id=keep_book_id,
                                chapter_id=new_chapter_id,
                                dryrun=dryrun
                            )
                            if new_page:
                                copied_pages += 1
                                # Update chapter_id if needed (BookStack API quirk)
                                if new_chapter_id:
                                    update_bookstack_page(new_page['id'], chapter_id=new_chapter_id, dryrun=dryrun)
                                adaptive_sleep()
                        
                        print(f'      Copied {len(chapter_id_map)} chapters and {copied_pages} pages')
                        
                        # Mark book for deletion
                        books_to_delete_after_creation.append(book_id)
                        
                        # Update page_to_book_map to point to the kept book
                        for conf_page_id, mapped_book_id in list(page_to_book_map.items()):
                            if mapped_book_id == book_id:
                                page_to_book_map[conf_page_id] = keep_book_id
                        
                        # Update existing_book_ids_in_shelf to use kept book instead
                        if book_id in existing_book_ids_in_shelf:
                            existing_book_ids_in_shelf.remove(book_id)
                            if keep_book_id not in existing_book_ids_in_shelf:
                                existing_book_ids_in_shelf.append(keep_book_id)
            
            # Delete merged books
            if books_to_delete_after_creation:
                print(f'\nDeleting {len(books_to_delete_after_creation)} merged duplicate books...')
                for book_id in books_to_delete_after_creation:
                    print(f'  Deleting book ID {book_id}...')
                    url = f'{BOOKSTACK_BASE_URL}/api/books/{book_id}'
                    response = requests.delete(url, headers=bookstack_headers)
                    if response.status_code == 429:
                        handle_rate_limit()
                        response = requests.delete(url, headers=bookstack_headers)
                    if response.status_code in [200, 204]:
                        print(f'    Successfully deleted book {book_id}')
                    else:
                        print(f'    Warning: Could not delete book {book_id}: {response.status_code}')
                    adaptive_sleep()
            
            if not duplicates_found:
                print('  No duplicates found in this run.')
        
        # After processing all books, update the shelf with all books at once
        if not dryrun and actual_shelf_id:
            # Fetch shelf data if we don't have it yet
            if not shelf_data and shelf_url:
                shelf_response = requests.get(shelf_url, headers=bookstack_headers)
                if shelf_response.status_code == 200:
                    shelf_data = shelf_response.json()
                else:
                    print(f'\nWarning: Could not fetch shelf data to update: {shelf_response.status_code}')
            
            if shelf_data and shelf_url:
                print(f'\nAdding all {len(existing_book_ids_in_shelf)} books to shelf {actual_shelf_id}...')
                update_payload = {
                    'name': shelf_data.get('name', ''),
                    'description': shelf_data.get('description', ''),
                    'books': existing_book_ids_in_shelf
                }
                update_response = requests.put(shelf_url, headers=bookstack_headers, json=update_payload)
                if update_response.status_code in [200, 201]:
                    # Note: BookStack API doesn't support setting shelf_id on books directly.
                    # The shelf relationship is managed entirely through the shelf's books array.
                    # Verify the update actually worked
                    time.sleep(0.5)  # Brief delay to ensure update is processed
                    verify_response = requests.get(shelf_url, headers=bookstack_headers)
                    if verify_response.status_code == 200:
                        verify_data = verify_response.json()
                        actual_books_in_shelf = verify_data.get('books', [])
                        actual_book_ids = [b.get('id') for b in actual_books_in_shelf]
                        if len(actual_book_ids) == len(existing_book_ids_in_shelf) and set(actual_book_ids) == set(existing_book_ids_in_shelf):
                            print(f'  Successfully added {len(existing_book_ids_in_shelf)} books to shelf {actual_shelf_id} (verified)')
                        else:
                            print(f'  Warning: Shelf update reported success but verification failed')
                            print(f'    Expected {len(existing_book_ids_in_shelf)} books, found {len(actual_book_ids)} books')
                            print(f'    Retrying update...')
                            # Retry once
                            retry_response = requests.put(shelf_url, headers=bookstack_headers, json=update_payload)
                            if retry_response.status_code in [200, 201]:
                                time.sleep(0.5)  # Brief delay after retry
                                retry_verify = requests.get(shelf_url, headers=bookstack_headers)
                                if retry_verify.status_code == 200:
                                    retry_data = retry_verify.json()
                                    retry_book_ids = [b.get('id') for b in retry_data.get('books', [])]
                                    if len(retry_book_ids) == len(existing_book_ids_in_shelf) and set(retry_book_ids) == set(existing_book_ids_in_shelf):
                                        print(f'  Retry successful - {len(retry_book_ids)} books now in shelf')
                                    else:
                                        print(f'  Retry verification failed: expected {len(existing_book_ids_in_shelf)}, found {len(retry_book_ids)}')
                                else:
                                    print(f'  Retry successful but could not verify')
                            else:
                                print(f'  Retry failed: {retry_response.status_code} - {retry_response.text}')
                    else:
                        print(f'  Warning: Could not verify shelf update: {verify_response.status_code}')
                        print(f'  Shelf update reported success, but verification failed')
                else:
                    print(f'  Warning: Could not update shelf: {update_response.status_code} - {update_response.text}')
            elif not shelf_data:
                print(f'\nWarning: Cannot update shelf - shelf data not available')
        elif dryrun:
            print(f'\n[DRYRUN] Would add {len(existing_book_ids_in_shelf)} books to shelf {shelf_id}')
        
        # Map all pages to their book (based on nearest book ancestor)
        print('\nMapping pages to their books...')
        # Create a set of page IDs that are books (0, 1, or 2 ancestors)
        book_page_ids = {page['id'] for page in top_level_pages}
        
        total_pages = len(confluence_pages)
        for idx, page in enumerate(confluence_pages, 1):
            if idx % 50 == 0 or idx == total_pages:
                print(f'  Mapping page {idx}/{total_pages}...')
            page_id = page['id']
            ancestors = page.get('ancestors', [])
            
            # If this page is itself a book, it's already mapped
            if page_id in book_page_ids:
                continue
            
            # Walk up the ancestor chain to find the nearest book
            # Start from the immediate parent and work up
            found_book = False
            for ancestor in reversed(ancestors):  # Start from immediate parent
                ancestor_id = ancestor['id']
                if ancestor_id in book_page_ids:
                    # This ancestor is a book, use it
                    if ancestor_id in page_to_book_map:
                        page_to_book_map[page_id] = page_to_book_map[ancestor_id]
                        found_book = True
                        break
            
            # If no book ancestor found, try the root ancestor
            if not found_book and ancestors:
                root_ancestor_id = ancestors[0]['id']
                # Walk up to find any book
                current_id = root_ancestor_id
                while current_id in confluence_pages_map:
                    if current_id in book_page_ids and current_id in page_to_book_map:
                        page_to_book_map[page_id] = page_to_book_map[current_id]
                        found_book = True
                        break
                    current_page = confluence_pages_map[current_id]
                    current_ancestors = current_page.get('ancestors', [])
                    if not current_ancestors:
                        break
                    current_id = current_ancestors[0]['id']
        
        print(f'Mapped {len([p for p in page_to_book_map.values() if p])} pages to books')
    
    # Build cache of existing BookStack pages (for all books if shelf mode, or single book if legacy)
    print('\nBuilding cache of existing BookStack pages...')
    if use_shelf_mode:
        # Fetch pages from all books in the shelf
        all_existing_books = fetch_bookstack_books()
        all_books = [b['id'] for b in all_existing_books if actual_shelf_id and b.get('shelf_id') == int(actual_shelf_id)]
        all_books.extend([b for b in page_to_book_map.values() if isinstance(b, int)])
        all_books = list(set(all_books))  # Deduplicate
        
        bookstack_pages = []
        print(f'Fetching pages from {len(all_books)} books...')
        for idx, book_id in enumerate(all_books, 1):
            print(f'  Fetching pages from book {idx}/{len(all_books)} (ID: {book_id})...')
            pages = fetch_bookstack_pages(book_id=str(book_id))
            bookstack_pages.extend(pages)
    else:
        bookstack_pages = fetch_bookstack_pages(book_id=book_id)
    
    existing_by_confluence_id = {}
    for page in bookstack_pages:
        existing_by_confluence_id[page['name']] = page
    
    print(f'Found {len(existing_by_confluence_id)} existing pages in BookStack.')
    
    # Build cache of existing BookStack chapters
    print('\nBuilding cache of existing BookStack chapters...')
    if use_shelf_mode:
        existing_chapters = []
        for book_id in all_books:
            chapters = fetch_bookstack_chapters(book_id)
            existing_chapters.extend(chapters)
    else:
        existing_chapters = fetch_bookstack_chapters(book_id)
    
    existing_chapter_names = {chapter['name'].lower(): chapter for chapter in existing_chapters}
    print(f'Found {len(existing_chapter_names)} existing chapters in BookStack.')
    
    # Process pages
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    
    # Build a map of Confluence page IDs to pages for hierarchy
    confluence_pages_map = {page['id']: page for page in confluence_pages}
    
    # Identify pages that have children (these will become chapters)
    print('Identifying pages with children...')
    pages_with_children = set()
    total_pages = len(confluence_pages)
    for idx, page in enumerate(confluence_pages, 1):
        if idx % 50 == 0 or idx == total_pages:
            print(f'  Checking page {idx}/{total_pages}...')
        page_id = page['id']
        # Check if any other page has this page as an ancestor
        for other_page in confluence_pages:
            ancestors = other_page.get('ancestors', [])
            if ancestors and ancestors[-1]['id'] == page_id:
                pages_with_children.add(page_id)
                break
    
    print(f'Found {len(pages_with_children)} pages with children (will become chapters)')
    
    # Maps for tracking what we've created
    page_to_chapter_map = {}  # Map Confluence page ID -> BookStack chapter ID (for pages that became chapters)
    page_to_page_map = {}     # Map Confluence page ID -> BookStack page ID (for intro pages in chapters)
    chapter_map = {}           # Map Confluence page ID -> BookStack chapter ID
    
    # Sort pages by depth (root pages first)
    def get_depth(page):
        ancestors = page.get('ancestors', [])
        return len(ancestors)
    
    sorted_pages = sorted(confluence_pages, key=get_depth)
    total_pages = len(sorted_pages)
    print(f'\nProcessing {total_pages} pages...\n')
    
    for idx, page in enumerate(sorted_pages, 1):
        page_id = page['id']
        page_title = page['title']
        
        print(f'[{idx}/{total_pages}] Processing {page_id}: {page_title[:50]}...')
        
        # Check if already exists (by name for now)
        existing_page = existing_by_confluence_id.get(page_title)
        
        if existing_page and skip_existing:
            print(f'  Skipping - already exists as page {existing_page["id"]}')
            skipped += 1
            page_to_page_map[page_id] = existing_page['id']
            continue
        
        # Get page content (HTML)
        body = page.get('body', {})
        storage = body.get('storage', {})
        html_content = convert_atlassian_storage_to_html(storage)
        
        # BookStack requires HTML content, so provide empty HTML if content is empty
        if not html_content or html_content.strip() == '':
            html_content = '<p></p>'  # Empty paragraph to satisfy BookStack requirement
        
        # Determine owner from page creator
        owner_id = None
        if user_map:
            history = page.get('history', {})
            created_by = history.get('createdBy', {})
            
            # Try multiple ways to extract email from Confluence API response
            creator_email = None
            if created_by:
                # Try nested user object first
                user_obj = created_by.get('user', {})
                if user_obj:
                    creator_email = user_obj.get('emailAddress', '') or user_obj.get('email', '')
                
                # If not found, try direct on createdBy
                if not creator_email:
                    creator_email = created_by.get('emailAddress', '') or created_by.get('email', '')
                
                # Try accountId and look up user
                if not creator_email and created_by.get('accountId'):
                    # Could potentially look up user by accountId, but email is preferred
                    pass
            
            if creator_email:
                owner_id = user_map.get(creator_email.lower())
                if owner_id:
                    print(f'  Assigned to user: {creator_email}')
                else:
                    # Debug: show what email we found but couldn't match
                    print(f'  Could not match creator email: {creator_email} (not in user_map)')
            else:
                # If no email, try to match by display name using user_email_mapping
                if created_by:
                    display_name = created_by.get('displayName', '')
                    # Remove "(Unlicensed)" suffix if present
                    display_name = display_name.replace(' (Unlicensed)', '').strip()
                    
                    if display_name and display_name in user_email_mapping:
                        mapped_email = user_email_mapping[display_name].lower()
                        owner_id = user_map.get(mapped_email)
                        if owner_id:
                            print(f'  Assigned to user via display name mapping: {display_name} -> {mapped_email}')
                        else:
                            print(f'  Found display name "{display_name}" in mapping but email {mapped_email} not in BookStack users')
                    else:
                        # Debug: show that we couldn't find email or display name match
                        print(f'  No email found and no display name match: {display_name}')
        
        # Determine which book this page belongs to
        target_book_id = None
        if use_shelf_mode:
            target_book_id = page_to_book_map.get(page_id)
            if not target_book_id:
                print(f'  Warning: Could not determine book for page {page_id}, skipping')
                errors += 1
                continue
        else:
            target_book_id = book_id
        
        # Determine parent chapter in BookStack by walking up the ancestor chain
        # to find the nearest ancestor that became a chapter
        parent_chapter_id = None
        ancestors = page.get('ancestors', [])
        if ancestors:
            # Walk through ancestors from most recent (immediate parent) to oldest (root ancestor)
            # We want to find the first ancestor that became a chapter
            for ancestor in reversed(ancestors):
                ancestor_id = ancestor.get('id')
                if not ancestor_id:
                    continue
                
                # Check if this ancestor became a chapter
                found_chapter_id = chapter_map.get(ancestor_id)
                if not found_chapter_id:
                    # Try string/int variants
                    found_chapter_id = chapter_map.get(str(ancestor_id))
                if not found_chapter_id and isinstance(ancestor_id, (str, int)):
                    try:
                        alt_id = int(ancestor_id) if isinstance(ancestor_id, str) else str(ancestor_id)
                        found_chapter_id = chapter_map.get(alt_id)
                    except (ValueError, TypeError):
                        pass
                
                if found_chapter_id:
                    parent_chapter_id = found_chapter_id
                    print(f'  Found ancestor chapter: Confluence ID {ancestor_id} -> BookStack Chapter ID {parent_chapter_id}')
                    break
            
            if not parent_chapter_id:
                # None of the ancestors became a chapter, this page will be at the book level
                print(f'  No ancestor chapter found - page will be at book level')
        
        # If this page has children, it becomes a chapter
        if page_id in pages_with_children:
            # Check if a chapter with this name already exists
            chapter_name = page_title
            if chapter_name.lower() in existing_chapter_names:
                # Get parent page name to prepend
                parent_page_name = None
                if ancestors:
                    # Get the immediate parent (last in ancestors list)
                    parent_ancestor = ancestors[-1]
                    parent_page_id = parent_ancestor.get('id')
                    if parent_page_id:
                        parent_page = confluence_pages_map.get(parent_page_id)
                        if parent_page:
                            parent_page_name = parent_page.get('title', 'Parent')
                
                if parent_page_name:
                    chapter_name = f"{parent_page_name} - {page_title}"
                    print(f'  Chapter name "{page_title}" already exists, using "{chapter_name}"')
                else:
                    # Fallback if we can't find parent name
                    chapter_name = f"Chapter - {page_title}"
                    print(f'  Chapter name "{page_title}" already exists, using "{chapter_name}"')
            
            # Create a chapter for this page
            if dryrun:
                print(f'  [DRYRUN] Would create chapter "{chapter_name}" and intro page')
                chapter_map[page_id] = 'dryrun_chapter_id'
                chapter_map[str(page_id)] = 'dryrun_chapter_id'
                created += 1
            else:
                # Create chapter
                chapter_result = create_bookstack_chapter(
                    name=chapter_name,
                    description='',  # Could use page content summary
                    book_id=target_book_id,
                    dryrun=dryrun
                )
                if chapter_result:
                    chapter_id = chapter_result.get('id')
                    chapter_map[page_id] = chapter_id
                    chapter_map[str(page_id)] = chapter_id
                    if isinstance(page_id, (str, int)):
                        try:
                            alt_id = int(page_id) if isinstance(page_id, str) else str(page_id)
                            chapter_map[alt_id] = chapter_id
                        except (ValueError, TypeError):
                            pass
                    # Update existing chapters cache with the new chapter
                    existing_chapter_names[chapter_name.lower()] = chapter_result
                    print(f'  Created chapter {chapter_id} for "{chapter_name}"')
                    if not dryrun:
                        adaptive_sleep()  # Adaptive rate limit protection
                    
                    # Create an "Introduction" page in this chapter with the parent page's content
                    intro_result = create_bookstack_page(
                        name='Introduction',
                        html=html_content,
                        book_id=target_book_id,
                        chapter_id=chapter_id,
                        owner_id=owner_id,
                        dryrun=dryrun
                    )
                    if intro_result:
                        intro_page_id = intro_result.get('id')
                        print(f'  Created intro page {intro_page_id} in chapter {chapter_id}')
                        page_to_page_map[page_id] = intro_page_id
                        # Always update chapter_id and owner_id after creation, because BookStack API
                        # may not properly set them during creation
                        needs_update = False
                        update_chapter_id = None
                        update_owner_id = None
                        if chapter_id:
                            needs_update = True
                            update_chapter_id = chapter_id
                        if owner_id:
                            needs_update = True
                            update_owner_id = owner_id
                        if needs_update:
                            update_result = update_bookstack_page(
                                intro_page_id,
                                chapter_id=update_chapter_id,
                                owner_id=update_owner_id,
                                dryrun=dryrun
                            )
                            if update_result:
                                if update_chapter_id:
                                    print(f'  Updated intro page to chapter {update_chapter_id}')
                                if update_owner_id:
                                    print(f'  Updated intro page owner to user {update_owner_id}')
                    created += 1
                    if not dryrun:
                        adaptive_sleep()  # Adaptive rate limit protection
                else:
                    errors += 1
        else:
            # This is a regular page (no children) - create it in the parent's chapter if it has one
            if dryrun:
                print(f'  [DRYRUN] Would create page in chapter {parent_chapter_id}')
                created += 1
            else:
                result = create_bookstack_page(
                    name=page_title,
                    html=html_content,
                    book_id=target_book_id,
                    chapter_id=parent_chapter_id,  # Use chapter_id instead of parent_id
                    owner_id=owner_id,
                    dryrun=dryrun
                )
                if result:
                    bookstack_page_id = result.get('id')
                    print(f'  Created page {bookstack_page_id} in chapter {parent_chapter_id}')
                    # Always update chapter_id and owner_id after creation, because BookStack API
                    # may not properly set them during creation
                    needs_update = False
                    update_chapter_id = None
                    update_owner_id = None
                    if parent_chapter_id:
                        needs_update = True
                        update_chapter_id = parent_chapter_id
                    if owner_id:
                        needs_update = True
                        update_owner_id = owner_id
                    if needs_update:
                        update_result = update_bookstack_page(
                            bookstack_page_id, 
                            chapter_id=update_chapter_id,
                            owner_id=update_owner_id, 
                            dryrun=dryrun
                        )
                        if update_result:
                            if update_chapter_id:
                                print(f'  Updated page to chapter {update_chapter_id}')
                            if update_owner_id:
                                print(f'  Updated page owner to user {update_owner_id}')
                    created += 1
                    page_to_page_map[page_id] = bookstack_page_id
                    existing_by_confluence_id[page_title] = result
                    if not dryrun:
                        adaptive_sleep()  # Adaptive rate limit protection
                else:
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


def sync_confluence_spaces(dryrun=False, skip_existing=True, space_key=None):
    """Sync a specific Confluence space to BookStack (as a shelf)."""
    print('\n' + '=' * 60)
    print('SYNC SPACES: Migrating Confluence space to BookStack shelf')
    print('=' * 60)
    
    space_key = space_key or CONFLUENCE_SPACE_KEY
    
    if not space_key:
        print('Error: CONFLUENCE_SPACE_KEY must be specified in .env or as parameter')
        return
    
    # Fetch the specific Confluence space
    confluence_spaces = fetch_confluence_spaces(space_key=space_key)
    
    if not confluence_spaces:
        print(f'Error: Space {space_key} not found in Confluence')
        return
    
    # Build cache of existing BookStack shelves
    print('\nBuilding cache of existing BookStack shelves...')
    bookstack_shelves = fetch_bookstack_shelves()
    existing_by_name = {name: shelf for name, shelf in bookstack_shelves.items()}
    print(f'Found {len(existing_by_name)} existing shelves in BookStack.')
    
    # Process the single space
    space = confluence_spaces[0]  # We only have one space
    space_key = space['key']
    space_name = space['name']
    space_description = space.get('description', {}).get('plain', {}).get('value', '')
    
    print(f'\nProcessing space {space_key}: {space_name}...')
    
    # Check if already exists
    existing_shelf = existing_by_name.get(space_name)
    
    created = 0
    skipped = 0
    errors = 0
    
    if existing_shelf and skip_existing:
        print(f'  Skipping - already exists as shelf {existing_shelf["id"]}')
        skipped += 1
    elif dryrun:
        if existing_shelf:
            print(f'  [DRYRUN] Would update shelf {existing_shelf["id"]}')
        else:
            print(f'  [DRYRUN] Would create new shelf: {space_name}')
        created += 1
    else:
        try:
            result = create_bookstack_shelf(
                name=space_name,
                description=space_description,
                dryrun=dryrun
            )
            if result:
                print(f'  Created shelf {result["id"]}')
                created += 1
            else:
                errors += 1
        except Exception as e:
            print(f'  Error: {e}')
            errors += 1
    
    # Summary
    print('\n' + '=' * 60)
    print('SYNC SPACES SUMMARY')
    print('=' * 60)
    print(f'Space processed: {space_name}')
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
  python migrate.py confluence --delete-pages --dryrun  # Delete all pages from book
  python migrate.py confluence --delete-pages            # Actually delete pages
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
    mode_group.add_argument('--delete-pages', action='store_true',
                            help='Delete all pages from a BookStack book (Confluence only)')
    
    # User sync mode (works with both jira and confluence)
    mode_group.add_argument('--sync-users', action='store_true',
                            help='Sync users to BookStack from specified source (use with --user-source)')
    
    # Options
    parser.add_argument('--dryrun', action='store_true',
                        help='Preview changes without making them')
    parser.add_argument('--update-existing', action='store_true',
                        help='Update existing items (default: skip)')
    parser.add_argument('--issues', type=str,
                        help='Comma-separated list of specific Jira issue keys to process')
    parser.add_argument('--user-source', choices=['atlassian', 'openproject'],
                        help='Source for user sync: atlassian (from Jira/Confluence) or openproject (from OpenProject). Required when using --sync-users.')
    parser.add_argument('--page-id', type=str,
                        help='Confluence page ID to sync (Confluence only, use with --sync-pages)')
    parser.add_argument('--page-title', type=str,
                        help='Confluence page title to sync (Confluence only, use with --sync-pages)')
    
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
        print(f'BookStack Shelf ID: {BOOKSTACK_SHELF_ID or "Not set"}')
        print(f'BookStack Book ID: {BOOKSTACK_BOOK_ID or "Not set"} (legacy mode)')
    
    if args.dryrun:
        print('\n*** DRY RUN MODE - No changes will be made ***\n')
    
    # Execute selected mode
    if args.sync_users:
        # User sync works for both jira and confluence types
        if not args.user_source:
            print('Error: --user-source is required when using --sync-users')
            print('  Use --user-source atlassian to import from Atlassian (Jira/Confluence)')
            print('  Use --user-source openproject to import from OpenProject')
            return 1
        
        # Validate BookStack credentials
        if not all([BOOKSTACK_BASE_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET]):
            print('Error: BookStack credentials not configured in .env')
            print('Required: BOOKSTACK_HOST, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET')
            return 1
        
        # Validate source credentials
        if args.user_source == 'atlassian':
            if not all([CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN]):
                print('Error: Atlassian credentials not configured in .env')
                print('Required: CONFLUENCE_HOST, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN')
                return 1
        elif args.user_source == 'openproject':
            if not all([OP_BASE_URL, OP_API_KEY]):
                print('Error: OpenProject credentials not configured in .env')
                print('Required: OPENPROJECT_HOST, OPENPROJECT_API_KEY')
                return 1
        
        sync_users_to_bookstack(
            source=args.user_source,
            dryrun=args.dryrun,
            skip_existing=not args.update_existing
        )
        return 0
    
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
        # Build user map for page assignment if users are available
        user_map = None
        try:
            bookstack_users = fetch_bookstack_users()
            user_map = {user.get('email', '').lower(): user.get('id') for user in bookstack_users if user.get('email')}
        except Exception as e:
            print(f'Warning: Could not load user map: {e}')
            print('  Pages will be created without owner assignment.')
        
        if args.sync_pages:
            sync_confluence_pages(
                dryrun=args.dryrun,
                skip_existing=not args.update_existing,
                space_key=CONFLUENCE_SPACE_KEY,
                shelf_id=int(BOOKSTACK_SHELF_ID) if BOOKSTACK_SHELF_ID else None,
                book_id=int(BOOKSTACK_BOOK_ID) if BOOKSTACK_BOOK_ID else None,
                user_map=user_map,
                page_id=args.page_id,
                page_title=args.page_title
            )
        elif args.sync_spaces:
            sync_confluence_spaces(
                dryrun=args.dryrun,
                skip_existing=not args.update_existing
            )
        elif args.delete_pages:
            delete_all_bookstack_pages(
                book_id=int(BOOKSTACK_BOOK_ID) if BOOKSTACK_BOOK_ID else None,
                dryrun=args.dryrun
            )
    
    return 0


if __name__ == '__main__':
    exit(main())
