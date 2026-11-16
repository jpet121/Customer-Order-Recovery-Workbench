# Architecture and operating scope

## Product

The browser displays the queue, the original source evidence, and the current correction. The Python HTTP service checks the actor, case revision, and data before it saves a change. A separate approver reviews the exact preview. The workbench records the approved payload fingerprint.

Send writes an outbox record and the case audit in one transaction. An outbox holds work that a worker still needs to send. Before each attempt, the worker reserves the next attempt in a separate journal. It sends the payload to the HTTP simulator and checks the receipt against a readback of the stored record.

The worker retries only after it has proof that the previous attempt made no write. It holds unknown outcomes for reconciliation. The journal and simulator remain outside workbench backup and restore. The restored workbench starts with access and delivery on hold until the operator verifies retention and reconciles the required evidence.

## Advisory behavior

The normal service starts with inference disabled. The offline fixture interface checks the input context, response schema, and deterministic fallback. Fixtures run in disposable local processes. The separate controlled inference test interface also checks authorization, cost reservations, retry limits, and an end-to-end deadline that includes fixture preparation.

Advisory output has no approval or delivery authority. The operator can continue the deterministic review workflow when advice is unavailable.

Use trusted local fixture code and trusted local backup files. The offline worker loads Python fixture code in its child process.

## Evaluation

The evaluation code scores supplied case, judgment, and attempt records. The workflow analyzer checks a study design in which groups try the workflows in a different order. Local tests exercise the scoring and record contracts with fixtures. Live model behavior and analyst study outcomes require separate evidence.

## Data

The demo source in [the application entry point](../iew/__main__.py) generates a fictional customer and a two-line order. It computes each CSV digest and places the digest in the matching JSON error manifest. It also seeds simulated original delivery evidence.

The workbench database holds source payloads, case state, and audit events. The simulator holds destination records. The attempt journal holds reservation and reconciliation history. Keep local backups and exports with the environment that generated them.
