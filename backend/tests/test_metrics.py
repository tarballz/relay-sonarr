"""Prometheus text exposition without a client library."""
import pytest

from app.obs import metrics as m


def test_counter_and_gauge_render_with_escaped_labels():
    reg = m.MetricsRegistry()
    c = reg.counter("relay_things_total", "Things seen.", ("kind",))
    g = reg.gauge("relay_temp", "A gauge.")
    c.inc(kind='say "hi"\n')
    c.inc(2, kind="plain")
    g.set(1.5)

    assert reg.render() == (
        "# HELP relay_things_total Things seen.\n"
        "# TYPE relay_things_total counter\n"
        'relay_things_total{kind="plain"} 2\n'
        'relay_things_total{kind="say \\"hi\\"\\n"} 1\n'
        "# HELP relay_temp A gauge.\n"
        "# TYPE relay_temp gauge\n"
        "relay_temp 1.5\n"
    )
    assert c.value(kind="plain") == 2


def test_histogram_buckets_are_cumulative():
    reg = m.MetricsRegistry()
    h = reg.histogram("relay_lat_seconds", "Latency.", ("instance",), buckets=(0.1, 1))
    h.observe(0.05, instance="4k")
    h.observe(0.5, instance="4k")
    h.observe(5, instance="4k")
    assert reg.render() == (
        "# HELP relay_lat_seconds Latency.\n"
        "# TYPE relay_lat_seconds histogram\n"
        'relay_lat_seconds_bucket{instance="4k",le="0.1"} 1\n'
        'relay_lat_seconds_bucket{instance="4k",le="1"} 2\n'
        'relay_lat_seconds_bucket{instance="4k",le="+Inf"} 3\n'
        'relay_lat_seconds_sum{instance="4k"} 5.55\n'
        'relay_lat_seconds_count{instance="4k"} 3\n'
    )


def test_wrong_labels_and_duplicate_names_raise():
    reg = m.MetricsRegistry()
    c = reg.counter("relay_x_total", "x", ("a",))
    with pytest.raises(ValueError):
        c.inc(b="1")
    with pytest.raises(ValueError):
        reg.counter("relay_x_total", "again")


def test_gauge_clear_drops_stale_series():
    reg = m.MetricsRegistry()
    g = reg.gauge("relay_eps", "eps", ("state",))
    g.set(3, state="wanted")
    g.clear()
    g.set(1, state="imported")
    assert 'state="wanted"' not in reg.render()


def test_standard_metrics_registered():
    text = m.METRICS.render()
    for name in ("relay_tick_total", "relay_sonarr_requests_total", "relay_events_total",
                 "relay_placement_episodes", "relay_sonarr_up"):
        assert f"# TYPE {name} " in text
