# Verification of the completeness and preservation changes

- 82 tests pass on Python 3.12 and a fresh Python 3.11 environment installed from
  both requirements files. The original test cases remain, with additional
  whole-course, retry, preservation, readability and concurrent-writer coverage.
- Python compilation, JavaScript syntax, shell syntax and `git diff --check` pass.
- Authenticated Canvas collection was exercised in disposable clones of two
  existing courses. One completed without errors; the other preserved collected
  material and correctly reported restricted Files, Pages and Quizzes endpoints.
  Its inaccessible New Quizzes endpoint was recorded as an optional unavailable
  source, without a failed retry task. Personal/group exports use deterministic
  account fixtures; they were not enabled against the live account.
- ZIP/form integration checks cover courses without modules, an unavailable module
  list, optional exports, partial recovery, exact network-call boundaries, cookie
  removal and exclusion of private state files. Browser tooling was unavailable;
  no visual verification is claimed.
- Existing archives were not cleaned or deduplicated. A scheduled refresh ran
  before sync was paused and updated 96 generated text exports. No attachments,
  locally authored files or protected files were deleted. After pausing sync,
  all 927 protected files remained hash-identical through implementation and tests.
- The existing connector was restarted and the automatic sync service restored.
  Local and public endpoints returned an HTTP 401 authentication challenge; the
  authenticated running connector exposed the same nine tools and the new inventory.
  Reboot persistence and new OAuth consent were not tested.

No new runtime packages, database, frontend build system, crawler, OCR or
transcription service were added. Legacy environments and document work products
remain intact; their removal requires checking their separate dependencies.
