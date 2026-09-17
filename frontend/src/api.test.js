import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api.js";

function mockFetch(response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api wire shapes", () => {
  it("api.pauseSeries sends POST with no body", async () => {
    const fetchMock = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({}),
    });

    await api.pauseSeries(7);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/series/7/pause");
    expect(init.method).toBe("POST");
    expect("body" in init).toBe(false);
    expect(init.headers["Content-Type"]).toBeUndefined();
  });

  it("api.removeSeries sends DELETE with deleteFiles query param and no body", async () => {
    const fetchMock = mockFetch({
      ok: true,
      status: 204,
      statusText: "No Content",
    });

    await api.removeSeries("1080p", 5, true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/instances/1080p/series/5?deleteFiles=true");
    expect(init.method).toBe("DELETE");
    expect("body" in init).toBe(false);
    expect(init.headers["Content-Type"]).toBeUndefined();
  });

  it("api.setDefaults sends PUT with JSON body and content-type", async () => {
    const fetchMock = mockFetch({
      ok: true,
      status: 200,
      json: async () => ({}),
    });

    await api.setDefaults({ minSeeders: 5 });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/settings/defaults");
    expect(init.method).toBe("PUT");
    expect(init.body).toBe(JSON.stringify({ minSeeders: 5 }));
    expect(init.headers["Content-Type"]).toBe("application/json");
  });
});
