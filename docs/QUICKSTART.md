# Run the local demo

These steps use PowerShell on Windows. The app runs two services on loopback. The simulator listens on port 8766. The workbench listens on port 8765 and starts its own delivery worker.

## Prepare the tools

Install Python 3.12, Node.js with npm, and PostgreSQL 16 with its client tools. Add the PostgreSQL client tools to `PATH`. Start a dedicated local PostgreSQL instance and use its operator account for setup. The examples below use the default port and the operator name `postgres`.

Run these commands from the repository folder.

```powershell
py -3.12 -m venv .venv
$python = (Resolve-Path ./.venv/Scripts/python.exe).Path
& $python -m pip install --only-binary=:all: -r requirements.txt
npm.cmd ci --ignore-scripts
npm.cmd run build
```

The build generates `web/static/app.js` and its source map from the TypeScript source.

## Create the databases and demo

Create three fresh databases. Keep the simulator and journal databases separate from the workbench database.

```powershell
createdb -h 127.0.0.1 -U postgres iew_workbench
createdb -h 127.0.0.1 -U postgres iew_simulator
createdb -h 127.0.0.1 -U postgres iew_journal

$env:IEW_WORKBENCH_DSN = "postgresql://postgres@127.0.0.1/iew_workbench"
$env:IEW_SIMULATOR_DSN = "postgresql://postgres@127.0.0.1/iew_simulator"
$env:IEW_JOURNAL_DSN = "postgresql://postgres@127.0.0.1/iew_journal"
$env:IEW_SIMULATOR_URL = "http://127.0.0.1:8766"
$env:IEW_SERVICE_TOKEN = (& $python -c "import secrets; print(secrets.token_hex(32))")

& $python -m iew init
& $python -m iew check-compatibility
& $python -m iew demo ./demo-files
```

The demo prompts for a local password of at least 12 characters. It creates fictional users and writes two pairs of CSV and manifest files. Enter a password that you use only for this demo. Keep database credentials and the service token in your local environment. [The configuration example](../.env.example) lists the settings that the CLI reads.

## Run with restricted database access

Create a runtime account with no database ownership. Configure its password through your local PostgreSQL authentication setup. Apply the grants with the operator connection strings before switching to the runtime account.

```powershell
createuser -h 127.0.0.1 -U postgres --no-superuser --no-createdb --no-createrole --pwprompt iew_runtime
& $python -m iew grant-runtime iew_runtime

$env:IEW_WORKBENCH_DSN = "postgresql://iew_runtime@127.0.0.1/iew_workbench"
$env:IEW_SIMULATOR_DSN = "postgresql://iew_runtime@127.0.0.1/iew_simulator"
$env:IEW_JOURNAL_DSN = "postgresql://iew_runtime@127.0.0.1/iew_journal"
```

Use the runtime account's PostgreSQL credentials for both services. Open a second PowerShell window from the configured shell so it inherits the connection settings and service token. Use the repository folder in both windows.

Run the simulator in the second window.

```powershell
& ./.venv/Scripts/python.exe -m iew simulator
```

Run the workbench in the original window.

```powershell
& ./.venv/Scripts/python.exe -m iew serve
```

Open [the local workbench](http://127.0.0.1:8765). Press Ctrl+C in each service window to stop it.

## Walk through the demo

1. Sign in as `analyst`. Import `demo-files/customer/customers.csv` with the `errors.json` from the same folder.
2. Open C100 and claim the case. Enter a reason and select the steward-C100 evidence. Select **Validate and submit preview**.
3. Sign out and sign in as `approver`. Open Approval inbox and select **Approve revision**. Open Delivery, select **Reconcile evidence**, then Send. Refresh the case to check the receipt.
4. Return as `analyst`. Import `demo-files/order/orders.csv` with its `errors.json`. Claim O200 and set the item reference on line 1 to `ITEM-1`. Select the matching evidence and submit the preview.
5. Return as `approver`. Approve, reconcile, and Send the order. Check that O200 shows **Confirmed** and has a receipt.
6. Use a failure group to request a repair packet. Sign in as `auditor` to download the redacted audit CSV.

Use the demo password when switching users. The customer must reach Confirmed before the order can use its destination customer reference.

## Run the source tests

Use a dedicated PostgreSQL test instance. Its test operator must be able to create and remove schemas, temporary databases, and roles. Install Chrome and Edge for the Playwright browser tests. Ensure that `pg_dump` and `pg_restore` are on `PATH`, or set `IEW_PG_BIN` to their folder.

```powershell
& $python -m pip install -r requirements-test.txt
npm.cmd run typecheck
npm.cmd run build
& $python -m mypy iew
$env:IEW_TEST_DSN = "postgresql://postgres@127.0.0.1/postgres"
& $python -m pytest -q
```

The test database setting is separate from the three runtime settings. Use the credentials and port for your dedicated test instance.
