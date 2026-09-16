"""The registry owns the download client the same way it owns Sonarr clients."""
from app.config import Config, DownloadClientConfig, InstanceConfig
from app.download.transmission import TransmissionClient
from app.sonarr.registry import Registry

INSTANCE = InstanceConfig(id="1080p", name="Sonarr 1080p",
                          url="http://10.0.0.1:8989", api_key="k")


def test_downloads_is_none_when_unconfigured():
    assert Registry(Config(instances=[INSTANCE])).downloads() is None


def test_downloads_builds_a_transmission_client():
    cfg = Config(instances=[INSTANCE], download_client=DownloadClientConfig(
        type="transmission", url="http://10.0.0.3:9091/transmission/rpc"))
    client = Registry(cfg).downloads()
    assert isinstance(client, TransmissionClient)
    assert client.base_url == "http://10.0.0.3:9091/transmission/rpc"


async def test_aclose_closes_the_download_client():
    cfg = Config(instances=[INSTANCE], download_client=DownloadClientConfig(
        type="transmission", url="http://10.0.0.3:9091/transmission/rpc"))
    registry = Registry(cfg)
    client = registry.downloads()
    client._client()  # materialise the pool so aclose has something to do
    await registry.aclose()
    assert client._http is None
