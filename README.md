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
- **Space Migration**: Convert Confluence spaces to BookStack books
- **Hierarchy Preservation**: Maintains parent-child relationships between pages
- **HTML Content**: Preserves formatted content from Confluence

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
BOOKSTACK_BOOK_ID=1                       # Your BookStack book ID (optional, for page migration)
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

**Note**: For page migration, you need to set `BOOKSTACK_BOOK_ID` in your `.env` file to specify which book to add pages to.

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

### Common Options

| Option | Description |
|--------|-------------|
| `--dryrun` | Preview changes without applying them |
| `--update-existing` | Update existing items (default: skip) |
| `--issues KEYS` | Comma-separated list of specific Jira issue keys |

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
2. **Migrate Spaces**: Run `python migrate.py confluence --sync-spaces --dryrun` to preview
3. **Create Books**: Run `python migrate.py confluence --sync-spaces` to create books
4. **Set Book ID**: Update `BOOKSTACK_BOOK_ID` in `.env` with the target book ID
5. **Preview Pages**: Run `python migrate.py confluence --sync-pages --dryrun` to preview
6. **Migrate Pages**: Run `python migrate.py confluence --sync-pages` to migrate pages
7. **Verify**: Check BookStack to confirm the migration

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

For Confluence to BookStack migrations, the tool attempts to preserve page hierarchies by processing root pages first, then children. If parent-child relationships aren't working correctly:

1. Ensure all parent pages are migrated before their children
2. Check that parent page IDs are correctly stored
3. Run with `--dryrun` to see the planned structure

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
