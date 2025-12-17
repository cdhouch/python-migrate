# Migration Tool - Jira to OpenProject / Confluence to BookStack

A Python-based migration tool for transferring data between platforms:
- **Jira to OpenProject**: Sync issues and preserve Epic hierarchies
- **Confluence to BookStack**: Sync pages and maintain page hierarchies

## Features

### Jira to OpenProject
- **Full Issue Migration**: Sync all issues from a Jira project to OpenProject
- **Epic Hierarchy**: Automatically assigns child issues to their parent Epics
- **Smart Matching**: Uses Jira ID custom field for exact matching, with fuzzy fallback
- **Incremental Sync**: Skip already-migrated issues or update them

### Confluence to BookStack
- **Page Migration**: Sync all pages from a Confluence space to BookStack
- **Space Migration**: Convert Confluence spaces to BookStack shelves
- **Hierarchy Preservation**: Maintains full hierarchy: Space → Shelf → Books (from top-level pages) → Chapters → Pages
- **HTML Content**: Preserves formatted content from Confluence
- **User Assignment**: Assigns pages to correct users based on Confluence page creators

### Common Features
- **Dry Run Mode**: Preview all changes before applying them
- **Comprehensive Diagnostics**: Debug unmatched items with detailed reports

## Prerequisites

- Python 3.8+
- Source system account with API access (Jira Cloud or Confluence)
- Target system instance with API access (OpenProject or BookStack)
- For Jira migrations: A custom text field in OpenProject to store Jira issue IDs

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/cdhouch/python-migrate.git
   cd python-migrate
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Copy the example environment file and configure it:
   ```bash
   cp .env.example .env
   ```

5. Edit `.env` with your credentials (see [Configuration](#configuration)).

## Configuration

### Environment Variables

Create a `.env` file with the following variables:

#### For Jira to OpenProject Migration

```bash
# Jira Configuration
JIRA_HOST=yourcompany.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_PROJECT_KEY=PROJ                    # Your Jira project key

# OpenProject Configuration
OPENPROJECT_HOST=https://your-openproject-instance.com
OPENPROJECT_API_KEY=your-openproject-api-key
OPENPROJECT_PROJECT_ID=3                  # Your OpenProject project ID
JIRA_ID_CUSTOM_FIELD=1                    # OpenProject custom field ID for Jira IDs
```

#### For Confluence to BookStack Migration

```bash
# Confluence Configuration
CONFLUENCE_HOST=yourcompany.atlassian.net
CONFLUENCE_EMAIL=your-email@example.com
CONFLUENCE_API_TOKEN=your-confluence-api-token
CONFLUENCE_SPACE_KEY=SPACE                # Your Confluence space key (optional)

# BookStack Configuration
BOOKSTACK_HOST=https://your-bookstack-instance.com
BOOKSTACK_TOKEN_ID=your-bookstack-token-id
BOOKSTACK_TOKEN_SECRET=your-bookstack-token-secret
BOOKSTACK_SHELF_ID=1                      # Your BookStack shelf ID (recommended: for new hierarchy)
BOOKSTACK_BOOK_ID=1                       # Your BookStack book ID (legacy: for single-book mode)
```

### Getting Your API Credentials

#### Jira/Confluence API Token
1. Go to [Atlassian Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click "Create API token"
3. Give it a descriptive label and copy the token

#### OpenProject API Key
1. Log into OpenProject
2. Go to My Account → Access Tokens
3. Generate a new API token

#### BookStack API Token
1. Log into BookStack
2. Go to Settings → API Tokens
3. Create a new API token and copy both the Token ID and Token Secret

### OpenProject Custom Field Setup (Jira migrations only)

Before migrating from Jira, create a custom field in OpenProject to store Jira issue IDs:

1. Go to Administration → Custom fields → Work packages
2. Create a new **Text** custom field named "Jira ID"
3. Enable it for all work package types you're migrating
4. Note the field ID (visible in the URL or field list)
5. Set `JIRA_ID_CUSTOM_FIELD` in your `.env` to this ID

### User Email Mapping (Confluence to BookStack migrations)

When importing users from Atlassian (Jira/Confluence) to BookStack, the API may not return email addresses for all users due to privacy settings. To ensure users are imported correctly, you can create a `user_map.json` file that maps display names to email addresses.

1. Copy the example file:
   ```bash
   cp user_map.json.example user_map.json
   ```

2. Edit `user_map.json` and add mappings for your users:
   ```json
   {
     "John Doe": "john.doe@example.com",
     "Jane Smith": "jane.smith@example.com"
   }
   ```

3. The mapping will be used when the API doesn't return email addresses. Users without emails (or without entries in the mapping) will be skipped.

**Note:** `user_map.json` is excluded from git (via `.gitignore`) to keep your user data private. The `user_map.json.example` file is included as a template.

## Usage

### Jira to OpenProject Migration

#### Sync Issues from Jira to OpenProject

```bash
# Preview what would be migrated (recommended first step)
python migrate.py jira --sync-issues --dryrun

# Migrate all issues (skip existing)
python migrate.py jira --sync-issues

# Migrate and update existing issues
python migrate.py jira --sync-issues --update-existing

# Migrate specific issues only
python migrate.py jira --sync-issues --issues ROE-123,ROE-456,ROE-789
```

#### Assign Children to Epics

After syncing issues, assign child issues to their parent Epics:

```bash
# Preview epic assignments
python migrate.py jira --assign-epics --dryrun

# Assign children to epics
python migrate.py jira --assign-epics
```

#### Diagnostics

```bash
# Show diagnostic info for unmatched Epics
python migrate.py jira --diagnose

# List all Epics in both systems
python migrate.py jira --list-epics
```

### Confluence to BookStack Migration

#### Sync Spaces to Books

First, migrate Confluence spaces to BookStack books:

```bash
# Preview what would be migrated
python migrate.py confluence --sync-spaces --dryrun

# Migrate all spaces to books
python migrate.py confluence --sync-spaces
```

#### Sync Pages from Confluence to BookStack

After creating books (or if using an existing book), sync pages:

```bash
# Preview what would be migrated
python migrate.py confluence --sync-pages --dryrun

# Migrate all pages (skip existing)
python migrate.py confluence --sync-pages

# Migrate and update existing pages
python migrate.py confluence --sync-pages --update-existing
```

**Note**: For page migration, you need to set either:
- `BOOKSTACK_SHELF_ID` (recommended): Uses new hierarchy where Confluence spaces become shelves, top-level pages become books
- `BOOKSTACK_BOOK_ID` (legacy): Uses single-book mode where all pages go into one book

## Command Reference

### Jira to OpenProject Commands

| Command | Description |
|---------|-------------|
| `python migrate.py jira --sync-issues` | Migrate issues from Jira to OpenProject |
| `python migrate.py jira --assign-epics` | Set parent-child relationships for Epics |
| `python migrate.py jira --diagnose` | Show diagnostics for unmatched Epics |
| `python migrate.py jira --list-epics` | List all Epics in both systems |

### Confluence to BookStack Commands

| Command | Description |
|---------|-------------|
| `python migrate.py confluence --sync-pages` | Migrate pages from Confluence to BookStack |
| `python migrate.py confluence --sync-spaces` | Migrate Confluence spaces to BookStack books |
| `python migrate.py confluence --delete-pages` | Delete all pages and chapters from a BookStack book |
| `python migrate.py confluence --sync-users --user-source atlassian` | Import users from Atlassian to BookStack |
| `python migrate.py jira --sync-users --user-source openproject` | Import users from OpenProject to BookStack |

### Common Options

| Option | Description |
|--------|-------------|
| `--dryrun` | Preview changes without applying them |
| `--update-existing` | Update existing items (default: skip) |
| `--issues KEYS` | Comma-separated list of specific Jira issue keys |
| `--user-source SOURCE` | Source for user sync: `atlassian` or `openproject` (required with `--sync-users`) |

## Type Mappings (Jira to OpenProject)

The tool maps Jira issue types to OpenProject work package types:

| Jira Type | OpenProject Type |
|-----------|------------------|
| Task | Task |
| Story | User story |
| Bug | Bug |
| Epic | Epic |
| Feature | Feature |
| Milestone | Milestone |
| Sub-task | Task |

## Status Mappings (Jira to OpenProject)

| Jira Status | OpenProject Status |
|-------------|-------------------|
| To Do | New |
| In Progress | In progress |
| Done | Closed |
| Closed | Closed |
| Resolved | Closed |

## Priority Mappings (Jira to OpenProject)

| Jira Priority | OpenProject Priority |
|---------------|---------------------|
| Highest | Immediate |
| High | High |
| Medium | Normal |
| Low | Low |
| Lowest | Low |

## Recommended Migration Workflows

### Jira to OpenProject

1. **Setup**: Configure `.env` with your credentials
2. **Preview**: Run `python migrate.py jira --sync-issues --dryrun` to preview migration
3. **Migrate**: Run `python migrate.py jira --sync-issues` to create work packages
4. **Preview Epics**: Run `python migrate.py jira --assign-epics --dryrun` to preview hierarchy
5. **Assign Epics**: Run `python migrate.py jira --assign-epics` to set parent relationships
6. **Verify**: Check OpenProject to confirm the migration

### Confluence to BookStack

1. **Setup**: Configure `.env` with your credentials
2. **Import Users** (Optional but recommended): 
   - Create `user_map.json` from `user_map.json.example` if needed (see User Email Mapping section)
   - Run `python migrate.py confluence --sync-users --user-source atlassian` to import users
3. **Migrate Spaces**: Run `python migrate.py confluence --sync-spaces --dryrun` to preview
4. **Create Books**: Run `python migrate.py confluence --sync-spaces` to create books
5. **Set Shelf ID** (recommended): Update `BOOKSTACK_SHELF_ID` in `.env` with the target shelf ID
   - Or **Set Book ID** (legacy): Update `BOOKSTACK_BOOK_ID` in `.env` for single-book mode
6. **Preview Pages**: Run `python migrate.py confluence --sync-pages --dryrun` to preview
7. **Migrate Pages**: Run `python migrate.py confluence --sync-pages` to migrate pages
8. **Verify**: Check BookStack to confirm the migration and user assignments

**New Hierarchy (with BOOKSTACK_SHELF_ID):**
- Confluence Space → BookStack Shelf
- Top-level Confluence pages → BookStack Books (within the shelf)
- Confluence pages with children → BookStack Chapters (within their book)
- Other Confluence pages → BookStack Pages (within their chapter or book)

**Legacy Mode (with BOOKSTACK_BOOK_ID):**
- All Confluence pages → Single BookStack Book
- Pages with children → BookStack Chapters
- Other pages → BookStack Pages

## Troubleshooting

### "No match found" for issues (Jira migrations)

The tool first tries to match by Jira ID custom field. If that fails, it uses fuzzy matching on the subject/summary. If you see many unmatched items:

1. Verify the custom field ID is correct in your `.env`
2. Check that issues were migrated with the Jira ID stored

### API Errors

- **401 Unauthorized**: Check your API credentials
- **403 Forbidden**: Verify API token permissions
- **404 Not Found**: Verify project IDs and host URLs

### Pagination Issues

The tool fetches all items with proper pagination. If you see incomplete results:

1. Check the total count reported
2. Verify there are no API rate limits being hit
3. Try running with `--dryrun` first to diagnose

### BookStack Page Hierarchy

For Confluence to BookStack migrations, the tool preserves page hierarchies by:
- Converting parent pages with children into BookStack chapters
- Assigning child pages to the appropriate ancestor chapter
- Processing pages in depth order (root pages first)

If parent-child relationships aren't working correctly:

1. Ensure all parent pages are migrated before their children
2. Check that parent page IDs are correctly stored
3. Run with `--dryrun` to see the planned structure

### User Email Mapping

When importing users from Atlassian, the API may not return email addresses due to privacy settings. To ensure all users are imported:

1. Copy `user_map.json.example` to `user_map.json`
2. Add mappings for your users in JSON format:
   ```json
   {
     "Display Name": "email@example.com",
     "John Doe": "john.doe@example.com"
   }
   ```
3. The mapping will be used when the API doesn't return email addresses
4. Users without emails (or without entries in the mapping) will be skipped

**Note:** `user_map.json` is excluded from git (via `.gitignore`) to keep your user data private.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Acknowledgments

This tool was created to provide a reliable Python-based alternative for migrating between different project management and documentation platforms, with a focus on preserving hierarchies and relationships accurately.
