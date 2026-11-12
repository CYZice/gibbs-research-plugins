from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from rq.exceptions import NoSuchJobError
from rq.job import JobStatus

from app.services import pipeline_dispatcher
from app.services.pipeline_dispatcher import (
    embedding_delivery_capacity,
    recover_missing_enqueued_deliveries,
    release_expired_delivery_leases,
    release_expired_run_leases,
    reserve_due_deliveries,
)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


def test_reserve_due_deliveries_uses_a_bounded_delivery_lease():
    task = SimpleNamespace(
        task_id="task_1",
        delivery_generation=2,
        delivery_status="pending",
        delivery_lease_until=None,
    )

    class Db:
        def scalars(self, _statement):
            return _Rows([task])

    now = datetime(2026, 7, 18, tzinfo=UTC)
    reservations = reserve_due_deliveries(
        Db(), "literature-parse", limit=1, lease_seconds=60, now=now
    )

    assert reservations[0].task_id == "task_1"
    assert reservations[0].job_id == "task_1-3"
    assert task.delivery_generation == 3
    assert task.delivery_status == "dispatching"
    assert task.delivery_lease_until == now + timedelta(seconds=60)


def test_reserve_due_deliveries_does_nothing_without_capacity():
    class Db:
        def scalars(self, _statement):
            raise AssertionError("no-capacity reservation must not query")

    assert reserve_due_deliveries(
        Db(), "literature-parse", limit=0, lease_seconds=60
    ) == []


def test_reservation_round_robins_across_batches():
    tasks = [
        SimpleNamespace(
            task_id="a1",
            batch_id="batch_a",
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        ),
        SimpleNamespace(
            task_id="a2",
            batch_id="batch_a",
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        ),
        SimpleNamespace(
            task_id="b1",
            batch_id="batch_b",
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        ),
    ]

    class Db:
        def scalars(self, _statement):
            return _Rows(tasks)

    reservations = reserve_due_deliveries(
        Db(),
        "literature-parse",
        limit=2,
        lease_seconds=60,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert [item.task_id for item in reservations] == ["a1", "b1"]


def test_reservation_uses_weighted_priority_quotas():
    tasks = [
        SimpleNamespace(
            task_id=f"interactive_{index}",
            batch_id=f"i_{index}",
            priority=100,
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        )
        for index in range(6)
    ] + [
        SimpleNamespace(
            task_id=f"normal_{index}",
            batch_id=f"n_{index}",
            priority=50,
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        )
        for index in range(4)
    ] + [
        SimpleNamespace(
            task_id=f"maintenance_{index}",
            batch_id=f"m_{index}",
            priority=10,
            delivery_generation=0,
            delivery_status="pending",
            delivery_lease_until=None,
        )
        for index in range(2)
    ]

    class Db:
        @staticmethod
        def scalars(_statement):
            return _Rows(tasks)

    reservations = reserve_due_deliveries(
        Db(),
        "literature-parse",
        limit=9,
        lease_seconds=60,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )
    ids = [item.task_id for item in reservations]

    assert sum(task_id.startswith("interactive") for task_id in ids) == 5
    assert sum(task_id.startswith("normal") for task_id in ids) == 3
    assert sum(task_id.startswith("maintenance") for task_id in ids) == 1


def test_expired_delivery_lease_is_returned_to_pending():
    executed = []

    class Db:
        def scalars(self, _statement):
            return _Rows(["task_1", "task_2"])

        def execute(self, statement):
            executed.append(statement)

    released = release_expired_delivery_leases(
        Db(), now=datetime(2026, 7, 18, tzinfo=UTC)
    )

    assert released == ["task_1", "task_2"]
    assert len(executed) == 1


def test_missing_redis_job_returns_enqueued_delivery_to_pending(monkeypatch):
    calls = []

    class Db:
        @staticmethod
        def execute(statement):
            calls.append(statement)
            if len(calls) == 1:
                return _Rows([("task_missing", 2), ("task_active", 4)])
            return _Rows([])

    def fetch(job_id, connection):
        assert connection == "redis"
        if job_id == "task_missing-2":
            raise NoSuchJobError
        return SimpleNamespace(
            get_status=lambda refresh=False: JobStatus.QUEUED
        )

    monkeypatch.setattr(pipeline_dispatcher.Job, "fetch", fetch)

    recovered = recover_missing_enqueued_deliveries(Db(), "redis")

    assert recovered == ["task_missing"]
    assert len(calls) == 2


def test_expired_run_lease_is_returned_to_retry_wait():
    executed = []

    class Db:
        def execute(self, statement):
            executed.append(statement)
            if len(executed) == 1:
                return _Rows([("task_1", "root_1", "run_1")])
            return _Rows([])

    released = release_expired_run_leases(
        Db(), now=datetime(2026, 7, 18, tzinfo=UTC)
    )

    assert released == ["task_1"]
    assert len(executed) == 4


def test_embedding_dispatch_capacity_is_minimum_of_workers_and_healthy_keys(
    monkeypatch,
):
    class Db:
        values = iter(
            [
                SimpleNamespace(provider_config_id="provider_embedding"),
                10,
            ]
        )

        @classmethod
        def scalar(cls, _statement):
            return next(cls.values)

    class Connection:
        @staticmethod
        def get(_key):
            return None

    workers = [
        SimpleNamespace(queue_names=lambda: ["literature-embedding"])
        for _ in range(12)
    ]
    monkeypatch.setattr(
        pipeline_dispatcher.Worker,
        "all",
        lambda connection: workers,
    )
    monkeypatch.setattr(
        pipeline_dispatcher,
        "Queue",
        lambda *_args, **_kwargs: SimpleNamespace(count=0),
    )
    monkeypatch.setattr(
        pipeline_dispatcher,
        "StartedJobRegistry",
        lambda *_args, **_kwargs: SimpleNamespace(count=0),
    )

    assert embedding_delivery_capacity(Connection(), Db()) == 10
