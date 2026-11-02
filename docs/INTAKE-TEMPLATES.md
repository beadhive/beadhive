# Intake templates and the information-sufficiency checklist

**Status:** Accepted · **Scope:** the GitHub issue forms under `.github/ISSUE_TEMPLATE/` and the
checklist the triage step reads to decide whether a report holds enough to act on.

This implements the "Information sufficiency" stage of
[external issue intake](design/external-issue-intake.md#3-triage-and-evidence-gathering):
triage evaluates a report against a **versioned template for its type** and asks only for the
facts it needs to decide, not every field mechanically. The forms are files in the repository;
nothing here writes to GitHub on its own.

## The forms

| Type | Form file | Marker value | Current version |
|---|---|---|---|
| Bug | `.github/ISSUE_TEMPLATE/bug_report.yml` | `bh-intake/bug/v1` | 1 |
| Feature | `.github/ISSUE_TEMPLATE/feature_request.yml` | `bh-intake/feature/v1` | 1 |

`.github/ISSUE_TEMPLATE/config.yml` keeps blank issues allowed (the behaviour before the forms
existed) and points security reports at the private advisory flow in `SECURITY.md`.

### Version marker

Every form carries a required `template_version` input, prefilled with the marker value above.
GitHub renders it into the issue body as a section:

```markdown
### Template version

bh-intake/bug/v1
```

Issue forms have no hidden fields, so the marker is a visible prefilled line that the form tells
the reporter to leave alone. Triage reads the first line matching
`bh-intake/<type>/v<N>` after the `### Template version` heading.

- A body with **no marker** (a blank issue, an old-style report, an edited-out marker) is
  *unversioned*: triage applies the **latest version of the closest type** and notes that it did.
- A marker for a version **older than current** is checked against **that version's** rows below,
  never the newest, so changing a form does not retroactively fail earlier reports.
- An **unknown** type or a version newer than this document is treated as unversioned.

### Changing a form

Bump the version when a field is added, removed, renamed, or its required-ness changes: edit the
form's header comment and `template_version` value, add a new version section below, and leave the
old section in place. Wording-only edits do not bump the version. A test keeps each form's field
ids, required-ness, and marker in step with this document.

## What "required" means

**Required** facts are the ones needed to *decide* what to do with the report. If one is missing
or unusable, triage asks for it specifically, says what investigation is blocked, and leaves the
report waiting for a reply. **Optional** facts speed things up but never block a decision; triage
does not ask for them unless a specific one becomes load-bearing. The forms enforce required
fields only for reporters who use them, so triage still checks the *content*: a required field
holding "see above" or a bare "n/a" counts as missing.

## Bug report, version 1

Marker: `bh-intake/bug/v1`.

| Field id | Form label | Need | What triage needs it for |
|---|---|---|---|
| `template_version` | Template version | Required | Selects this checklist. |
| `observed` | What happened | Required | The defect itself, with error text as printed. |
| `expected` | What you expected | Required | Separates a bug from a misunderstanding. |
| `steps` | Steps to reproduce, or evidence | Required | Decides reproduction: exact steps, or the log or output that stands in for them. |
| `version` | Affected version | Required | Finds duplicates and prior fixes, and whether a newer release already fixes it. |
| `impact` | Impact | Required | Priority: blocking versus cosmetic, workaround or none. |
| `environment` | Environment | Optional | Needed only when the behaviour looks platform or install dependent. |
| `impact_detail` | Impact detail | Optional | Scope and frequency; refines priority. |
| `additional` | Additional context | Optional | Regression hints and related reports. |

Sufficient to decide: `observed`, `expected`, a reproduction or evidence in `steps`, and a
`version`. Without `steps` content triage cannot choose between reproducing, accepting on
credible evidence, or rejecting, so it asks. Without `version` it cannot check for an existing
fix. A missing `environment` is asked for only if reproduction fails or the report looks
environment specific.

## Feature request, version 1

Marker: `bh-intake/feature/v1`. The field ids `observed`, `expected`, `evidence`, `version` and
`impact` are shared with the bug form so triage can use one set of readers.

| Field id | Form label | Need | What triage needs it for |
|---|---|---|---|
| `template_version` | Template version | Required | Selects this checklist. |
| `observed` | Current behaviour and the problem | Required | The gap, in terms of what beadhive does today. |
| `expected` | Proposed behaviour | Required | The change being asked for, concretely enough to assess. |
| `evidence` | Use case or evidence | Required | Whether the need is real, and design assessment rather than reproduction. |
| `version` | Version you looked at | Required | Whether the capability already exists or has changed since. |
| `impact` | Impact | Required | Priority: unblocks work, removes a workaround, or nice to have. |
| `alternatives` | Alternatives considered | Optional | Avoids re-proposing a workaround already tried. |
| `environment` | Environment | Optional | Only when the request depends on platform or tracker setup. |
| `impact_detail` | Who benefits | Optional | Breadth of the need; refines priority. |

Sufficient to decide: the problem, the proposed behaviour, a use case, and the version looked at.
Feature requests need design assessment, so there is no reproduction step; when `evidence` is a
bare restatement of the proposal, triage asks for a concrete scenario.

## How triage uses this

1. Read the marker; pick the type and version (or fall back as above).
2. Check each **Required** row against the issue body sections, which GitHub names by form label.
3. If every required fact is present and usable, move on to the reproduction decision.
4. Otherwise ask for the specific missing facts in one reply, name the investigation that is
   blocked, and wait. A reply wakes the report for reassessment; it does not by itself make the
   report ready.
5. Record which template and version were applied, so a later reader can see why the report was
   or was not judged sufficient.

Replies are drafts under the supervised flow: the operator approves each tracker write.
