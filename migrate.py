#!/usr/bin/env python3
"""
Jira to OpenProject Migration Tool

A Python-based migration tool that handles:
- Syncing issues from Jira to OpenProject
- Assigning children to their parent Epics
- Diagnostics for troubleshooting

Usage:
    python migrate.py --sync-issues [--dryrun]
    python migrate.py --assign-epics [--dryrun]
    python migrate.py --diagnose
    python migrate.py --list-epics
"""

import requests
import json
from dotenv import load_dotenv
import os
import argparse
import tempfile
import shutil
from fuzzywuzzy import fuzz

load_dotenv()

# =============================================================================
# Configuration
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
# Migration Functions
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


def sync_issues(dryrun=False, skip_existing=True, specific_keys=None):
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
    
    return existing_by_jira_id


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


def assign_epics(dryrun=False, diagnose=False):
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


def list_epics():
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
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Jira to OpenProject Migration Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python migrate.py --sync-issues --dryrun     # Preview issue migration
  python migrate.py --sync-issues              # Migrate all issues
  python migrate.py --assign-epics --dryrun    # Preview epic assignments
  python migrate.py --assign-epics             # Assign children to epics
  python migrate.py --diagnose                 # Show diagnostic info
  python migrate.py --list-epics               # List all epics
        """
    )
    
    # Mode selection
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--sync-issues', action='store_true',
                            help='Sync issues from Jira to OpenProject')
    mode_group.add_argument('--assign-epics', action='store_true',
                            help='Assign children to their parent Epics')
    mode_group.add_argument('--diagnose', action='store_true',
                            help='Show diagnostics for unmatched Epics')
    mode_group.add_argument('--list-epics', action='store_true',
                            help='List all Epics in Jira and OpenProject')
    
    # Options
    parser.add_argument('--dryrun', action='store_true',
                        help='Preview changes without making them')
    parser.add_argument('--update-existing', action='store_true',
                        help='Update existing work packages (default: skip)')
    parser.add_argument('--issues', type=str,
                        help='Comma-separated list of specific Jira issue keys to process')
    
    args = parser.parse_args()
    
    # Validate configuration
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
    
    if args.dryrun:
        print('\n*** DRY RUN MODE - No changes will be made ***\n')
    
    # Execute selected mode
    if args.sync_issues:
        specific_keys = args.issues.split(',') if args.issues else None
        sync_issues(
            dryrun=args.dryrun,
            skip_existing=not args.update_existing,
            specific_keys=specific_keys
        )
    elif args.assign_epics:
        assign_epics(dryrun=args.dryrun)
    elif args.diagnose:
        assign_epics(diagnose=True)
    elif args.list_epics:
        list_epics()
    
    return 0


if __name__ == '__main__':
    exit(main())

