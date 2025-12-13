# Jira to OpenProject Migration Tool

A Python-based migration tool for transferring issues from Jira Cloud to OpenProject, including full Epic hierarchy preservation.

## Features

- **Full Issue Migration**: Sync all issues from a Jira project to OpenProject
- **Epic Hierarchy**: Automatically assigns child issues to their parent Epics
- **Smart Matching**: Uses Jira ID custom field for exact matching, with fuzzy fallback
- **Incremental Sync**: Skip already-migrated issues or update them
- **Dry Run Mode**: Preview all changes before applying them
- **Comprehensive Diagnostics**: Debug unmatched items with detailed reports

## Prerequisites

- Python 3.8+
- Jira Cloud account with API access
- OpenProject instance with API access
- A custom text field in OpenProject to store Jira issue IDs

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/jira-openproject-migrate.git
   cd jira-openproject-migrate
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

```bash
# Jira Configuration
JIRA_HOST=yourcompany.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-jira-api-token

# OpenProject Configuration
OPENPROJECT_HOST=https://your-openproject-instance.com
OPENPROJECT_API_KEY=your-openproject-api-key

# Project Configuration
JIRA_PROJECT_KEY=PROJ                    # Your Jira project key
OPENPROJECT_PROJECT_ID=3                  # Your OpenProject project ID
JIRA_ID_CUSTOM_FIELD=1                    # OpenProject custom field ID for Jira IDs
```

### Getting Your API Credentials

#### Jira API Token
1. Go to [Atlassian Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click "Create API token"
3. Give it a descriptive label and copy the token

#### OpenProject API Key
1. Log into OpenProject
2. Go to My Account → Access Tokens
3. Generate a new API token

### OpenProject Custom Field Setup

Before migrating, create a custom field in OpenProject to store Jira issue IDs:

1. Go to Administration → Custom fields → Work packages
2. Create a new **Text** custom field named "Jira ID"
3. Enable it for all work package types you're migrating
4. Note the field ID (visible in the URL or field list)
5. Set `JIRA_ID_CUSTOM_FIELD` in your `.env` to this ID

## Usage

### Sync Issues from Jira to OpenProject

```bash
# Preview what would be migrated (recommended first step)
python migrate.py --sync-issues --dryrun

# Migrate all issues (skip existing)
python migrate.py --sync-issues

# Migrate and update existing issues
python migrate.py --sync-issues --update-existing

# Migrate specific issues only
python migrate.py --sync-issues --issues ROE-123,ROE-456,ROE-789
```

### Assign Children to Epics

After syncing issues, assign child issues to their parent Epics:

```bash
# Preview epic assignments
python migrate.py --assign-epics --dryrun

# Assign children to epics
python migrate.py --assign-epics
```

### Diagnostics

```bash
# Show diagnostic info for unmatched Epics
python migrate.py --diagnose

# List all Epics in both systems
python migrate.py --list-epics
```

## Command Reference

| Command | Description |
|---------|-------------|
| `--sync-issues` | Migrate issues from Jira to OpenProject |
| `--assign-epics` | Set parent-child relationships for Epics |
| `--diagnose` | Show diagnostics for unmatched items |
| `--list-epics` | List all Epics in both systems |
| `--dryrun` | Preview changes without applying them |
| `--update-existing` | Update existing work packages (default: skip) |
| `--issues KEYS` | Comma-separated list of specific issue keys |

## Type Mappings

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

## Status Mappings

| Jira Status | OpenProject Status |
|-------------|-------------------|
| To Do | New |
| In Progress | In progress |
| Done | Closed |
| Closed | Closed |
| Resolved | Closed |

## Priority Mappings

| Jira Priority | OpenProject Priority |
|---------------|---------------------|
| Highest | Immediate |
| High | High |
| Medium | Normal |
| Low | Low |
| Lowest | Low |

## Recommended Migration Workflow

1. **Setup**: Configure `.env` with your credentials
2. **Preview**: Run `--sync-issues --dryrun` to preview migration
3. **Migrate**: Run `--sync-issues` to create work packages
4. **Preview Epics**: Run `--assign-epics --dryrun` to preview hierarchy
5. **Assign Epics**: Run `--assign-epics` to set parent relationships
6. **Verify**: Check OpenProject to confirm the migration

## Troubleshooting

### "No match found" for issues

The tool first tries to match by Jira ID custom field. If that fails, it uses fuzzy matching on the subject/summary. If you see many unmatched items:

1. Run `--diagnose` to see closest matches
2. Verify the custom field ID is correct in your `.env`
3. Check that issues were migrated with the Jira ID stored

### API Errors

- **401 Unauthorized**: Check your API credentials
- **403 Forbidden**: Verify API token permissions
- **404 Not Found**: Verify project IDs and host URLs

### Pagination Issues

The tool fetches all work packages with proper pagination. If you see incomplete results:

1. Check the total count reported
2. Verify there are no API rate limits being hit
3. Try running with `--dryrun` first to diagnose

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

This tool was created to provide a reliable Python-based alternative for migrating from Jira to OpenProject, with a focus on preserving Epic hierarchies accurately.

