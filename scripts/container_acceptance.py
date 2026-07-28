#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )


def generated_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated container, migration, worker, and backup acceptance checks.",
    )
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:10]
    network = f"basis-hawk-acceptance-{suffix}"
    postgres = f"basis-hawk-postgres-{suffix}"
    api = f"basis-hawk-api-{suffix}"
    worker_container = f"basis-hawk-worker-{suffix}"
    backups = f"basis-hawk-backups-{suffix}"
    api_image = "basis-hawk-api:acceptance"
    backup_image = "basis-hawk-backup:acceptance"
    database_password = secrets.token_urlsafe(24)
    database_url = (
        "postgresql+asyncpg://basis_hawk:"
        f"{database_password}@{postgres}/basis_hawk"
    )
    credential_key = generated_key()
    backup_key = generated_key()

    def docker(*values: str, check: bool = True, capture: bool = False):
        return run(["docker", *values], check=check, capture=capture)

    def cleanup() -> None:
        docker(
            "rm",
            "-f",
            worker_container,
            api,
            postgres,
            check=False,
            capture=True,
        )
        docker("volume", "rm", backups, check=False, capture=True)
        docker("network", "rm", network, check=False, capture=True)

    cleanup()
    try:
        if not args.skip_build:
            docker("build", "-t", api_image, ".")
            docker(
                "build",
                "-f",
                "Dockerfile.backup",
                "-t",
                backup_image,
                ".",
            )

        docker("network", "create", network, capture=True)
        docker("volume", "create", backups, capture=True)
        docker(
            "run",
            "-d",
            "--name",
            postgres,
            "--network",
            network,
            "-e",
            "POSTGRES_DB=basis_hawk",
            "-e",
            "POSTGRES_USER=basis_hawk",
            "-e",
            f"POSTGRES_PASSWORD={database_password}",
            "postgres:17-alpine",
            capture=True,
        )
        for _ in range(60):
            ready = docker(
                "exec",
                postgres,
                "pg_isready",
                "-U",
                "basis_hawk",
                "-d",
                "basis_hawk",
                check=False,
                capture=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL did not become ready")

        docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            api_image,
            "alembic",
            "upgrade",
            "head",
        )
        docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            api_image,
            "alembic",
            "check",
        )
        revision = docker(
            "exec",
            postgres,
            "psql",
            "-At",
            "-U",
            "basis_hawk",
            "-d",
            "basis_hawk",
            "-c",
            "SELECT version_num FROM alembic_version;",
            capture=True,
        ).stdout.strip()
        if revision != "20260727_0028":
            raise RuntimeError(f"unexpected Alembic revision: {revision}")

        intent_persistence_probe = f"""
import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from basis_hawk.storage import Database

DATABASE_URL = {database_url!r}


async def main():
    database = Database(DATABASE_URL)
    now = datetime.now(UTC)
    intent_id = str(uuid.uuid4())
    intent, legs, created = await database.create_trade_intent(
        intent={{
            "id": intent_id,
            "idempotency_key": str(uuid.uuid4()),
            "request_fingerprint": "a" * 64,
            "exchange": "gate",
            "environment": "live",
            "base_asset": "ACCEPTANCE",
            "action": "open",
            "status": "failed",
            "leverage": 1,
            "requested_notional": Decimal("10"),
            "base_quantity": Decimal("1"),
            "spot_fee_rate": Decimal("0.001"),
            "perp_fee_rate": Decimal("0.0005"),
            "market_observed_at": now,
            "config_version": "b" * 64,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }},
        legs=[
            {{
                "id": str(uuid.uuid4()),
                "trade_intent_id": intent_id,
                "leg": market,
                "market": market,
                "symbol": "ACCEPTANCE_USDT",
                "side": side,
                "client_order_id": f"acceptance-{{market}}-{{intent_id}}",
                "status": "created",
                "quantity": Decimal("1"),
                "base_multiplier": Decimal("1"),
                "limit_price": Decimal("10"),
                "filled_quantity": Decimal("0"),
                "reduce_only": False,
                "created_at": now,
                "updated_at": now,
            }}
            for market, side in (("spot", "buy"), ("perp", "sell"))
        ],
    )
    assert created and intent.id == intent_id and len(legs) == 2
    await database.close()


asyncio.run(main())
"""
        docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            api_image,
            "python",
            "-c",
            intent_persistence_probe,
        )

        docker(
            "run",
            "-d",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--name",
            api,
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            "-e",
            "BASIS_HAWK_AUTH_REQUIRED=false",
            "-e",
            f"BASIS_HAWK_CREDENTIAL_MASTER_KEY={credential_key}",
            api_image,
            "sh",
            "-c",
            "exec basis-hawk serve",
            capture=True,
        )
        health_command = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready')"
        )
        for _ in range(60):
            health = docker(
                "exec",
                api,
                "python",
                "-c",
                health_command,
                check=False,
                capture=True,
            )
            if health.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("API readiness check did not pass")

        docker("restart", api, capture=True)
        for _ in range(60):
            health = docker(
                "exec",
                api,
                "python",
                "-c",
                health_command,
                check=False,
                capture=True,
            )
            if health.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("API did not recover after process restart")

        docker("restart", postgres, capture=True)
        for _ in range(60):
            ready = docker(
                "exec",
                postgres,
                "pg_isready",
                "-U",
                "basis_hawk",
                "-d",
                "basis_hawk",
                check=False,
                capture=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL did not recover after restart")
        database_probe = (
            "import urllib.request;"
            "urllib.request.urlopen("
            "'http://127.0.0.1:8000/api/system/execution'"
            ")"
        )
        for _ in range(30):
            probe = docker(
                "exec",
                api,
                "python",
                "-c",
                database_probe,
                check=False,
                capture=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("API database access did not recover after restart")

        docker(
            "run",
            "-d",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--name",
            worker_container,
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            "-e",
            f"BASIS_HAWK_CREDENTIAL_MASTER_KEY={credential_key}",
            api_image,
            "basis-hawk",
            "worker",
            capture=True,
        )
        advisory_lock_key = "7284217119035423281"
        for _ in range(120):
            lock_probe = docker(
                "exec",
                postgres,
                "psql",
                "-At",
                "-U",
                "basis_hawk",
                "-d",
                "basis_hawk",
                "-c",
                f"SELECT pg_try_advisory_lock({advisory_lock_key});",
                check=False,
                capture=True,
            )
            if lock_probe.returncode == 0 and lock_probe.stdout.strip() == "f":
                break
            worker_running = docker(
                "inspect",
                "-f",
                "{{.State.Running}}",
                worker_container,
                check=False,
                capture=True,
            )
            if (
                worker_running.returncode != 0
                or worker_running.stdout.strip() != "true"
            ):
                raise RuntimeError(
                    "the primary execution worker exited before acquiring its lock"
                )
            time.sleep(0.25)
        else:
            raise RuntimeError(
                "the primary execution worker did not acquire its lock in time"
            )
        competing_worker = docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            "-e",
            f"BASIS_HAWK_CREDENTIAL_MASTER_KEY={credential_key}",
            api_image,
            "basis-hawk",
            "worker",
            "--once",
            check=False,
            capture=True,
        )
        if competing_worker.returncode == 0 or "holds the account lock" not in (
            competing_worker.stdout + competing_worker.stderr
        ):
            raise RuntimeError("a competing execution worker was not rejected")
        docker("rm", "-f", worker_container, capture=True)

        worker = docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-e",
            f"BASIS_HAWK_DATABASE_URL={database_url}",
            "-e",
            f"BASIS_HAWK_CREDENTIAL_MASTER_KEY={credential_key}",
            api_image,
            "basis-hawk",
            "worker",
            "--once",
            capture=True,
        )
        if "worker: reconciliation" not in worker.stdout:
            raise RuntimeError("worker acceptance output was incomplete")

        docker(
            "exec",
            postgres,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "basis_hawk",
            "-d",
            "basis_hawk",
            "-c",
            (
                "INSERT INTO audit_events "
                "(id, occurred_at, event_type, actor, details) VALUES "
                "('acceptance-probe', now(), 'acceptance.probe', 'system', '{}');"
            ),
            capture=True,
        )
        backup_environment = [
            "-e",
            f"PGHOST={postgres}",
            "-e",
            "PGPORT=5432",
            "-e",
            "PGUSER=basis_hawk",
            "-e",
            f"PGPASSWORD={database_password}",
            "-e",
            f"BASIS_HAWK_BACKUP_KEY={backup_key}",
        ]
        docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-v",
            f"{backups}:/backups",
            *backup_environment,
            "-e",
            "PGDATABASE=basis_hawk",
            backup_image,
            "create",
            capture=True,
        )
        archive = docker(
            "run",
            "--rm",
            "-v",
            f"{backups}:/backups:ro",
            "--entrypoint",
            "find",
            "postgres:17-alpine",
            "/backups",
            "-maxdepth",
            "1",
            "-name",
            "*-daily.bhbk",
            "-print",
            "-quit",
            capture=True,
        ).stdout.strip()
        if not archive:
            raise RuntimeError("encrypted backup archive was not created")
        docker(
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-v",
            f"{backups}:/backups:ro",
            *backup_environment,
            "-e",
            "PGDATABASE=basis_hawk",
            backup_image,
            "verify",
            archive,
            capture=True,
        )
        docker(
            "exec",
            postgres,
            "createdb",
            "-U",
            "basis_hawk",
            "restored",
            capture=True,
        )
        restore_command = [
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--network",
            network,
            "-v",
            f"{backups}:/backups:ro",
            *backup_environment,
            "-e",
            "PGDATABASE=restored",
            backup_image,
            "restore",
            archive,
            "--confirmed",
        ]
        docker(*restore_command, capture=True)
        restored = docker(
            "exec",
            postgres,
            "psql",
            "-At",
            "-U",
            "basis_hawk",
            "-d",
            "restored",
            "-c",
            "SELECT count(*) FROM audit_events WHERE id='acceptance-probe';",
            capture=True,
        ).stdout.strip()
        if restored != "1":
            raise RuntimeError("restored database did not contain the acceptance probe")
        overwrite = docker(*restore_command, check=False, capture=True)
        if overwrite.returncode == 0:
            raise RuntimeError("restore unexpectedly overwrote a nonempty database")

        print("container image build: ok")
        print("PostgreSQL 17 migration: ok")
        print("PostgreSQL model/schema drift: none")
        print("PostgreSQL parent-before-leg persistence: ok")
        print("API readiness: ok")
        print("API process restart recovery: ok")
        print("PostgreSQL restart recovery: ok")
        print("single execution-worker lock: ok")
        print("single worker pass: ok")
        print("encrypted backup verification: ok")
        print("empty database restore: ok")
        print("nonempty restore refusal: ok")
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"container acceptance failed: {exc}", file=sys.stderr)
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
