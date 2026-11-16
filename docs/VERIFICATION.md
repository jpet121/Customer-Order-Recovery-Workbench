# Verification summary

Version 0.1.0 was verified for the local simulator milestone. The source commit is `0e53f5ae7c184ec023046caf132b95b9f562a82a`. The recorded executable build hash is `de7a6c48741a511af952235265bfdd987bd784c3d07efb5c8a4a9a8a1ec7265d`.

## Candidate results

| Check | Observed result |
| --- | --- |
| Windows engineering regression | 1,062 cases passed. Two supplemental cases also passed. |
| Independent engineering review | The reviewer exercised 1,064 distinct cases and issued a final pass for the local simulator milestone. |
| Client termination and recovery | The corrected test confirmed the actual database client PID. Both transaction variants and all 22 stress runs passed. |
| Controlled inference deadline | The engineering probe rejected the fixture with a slow serialization hook in 0.000783 seconds. The two-timeout diagnosis took 41.875 seconds. The diagnosis limit was 45 seconds. |
| Safety mutations | The tests caught all five declared mutations. |
| Measured line and branch coverage | The exact-build gate passed for 3,098 Python lines and 1,304 branches, plus 341 mapped TypeScript lines and 532 branches. |
| Browser | Windows Chrome and Edge ran browser tests. Nine Linux Chromium journeys passed. |

The original recovery run exposed an error in the test setup. On Windows the launcher PID differed from the database client PID. The reviewer preserved the failure, corrected the test, and reran it against the same candidate.

The measured coverage also includes specific SQL and CSS checks. Live model quality, independent accessibility qualification, and production operation require separate evidence. The source includes the local tests under [tests](../tests/). [Run the source tests](QUICKSTART.md#run-the-source-tests) to reproduce that suite with the documented prerequisites.
