import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./http.js";

function mockFetch(response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(status, body, { ok = status < 400 } = {}) {
  return {
    ok,
    status,
    statusText: `status ${status}`,
    json: async () => body,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("prefixes /api and returns parsed JSON", async () => {
    const fetchMock = mockFetch(jsonResponse(200, { ok: true }));
    await expect(request("/series")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/series");
  });

  it("serializes a JSON body and sets the content type", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    await request("/add", { method: "POST", body: { tvdbId: 7 } });
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.method).toBe("POST");
    expect(opts.body).toBe(JSON.stringify({ tvdbId: 7 }));
    expect(opts.headers["Content-Type"]).toBe("application/json");
  });

  it("sends no content type when there is no body", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    await request("/series");
    expect(fetchMock.mock.calls[0][1].headers["Content-Type"]).toBeUndefined();
  });

  it("returns null for 204 and never parses the body", async () => {
    const json = vi.fn();
    mockFetch({ ok: true, status: 204, statusText: "No Content", json });
    await expect(request("/thing", { method: "DELETE" })).resolves.toBeNull();
    expect(json).not.toHaveBeenCalled();
  });

  it("raises ApiError carrying the status and string detail", async () => {
    mockFetch(jsonResponse(404, { detail: "Series not found" }));
    const error = await request("/series/1").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.message).toBe("Series not found");
    expect(error.detail).toBe("Series not found");
    expect(error.fields).toEqual({});
  });

  it("maps a 422 validation body to fields", async () => {
    mockFetch(
      jsonResponse(422, {
        detail: [
          { loc: ["body", "minSeeders"], msg: "Input should be >= 0", type: "greater_than_equal" },
          { loc: ["body"], msg: "Extra inputs are not permitted", type: "extra_forbidden" },
        ],
      }),
    );
    const error = await request("/settings/defaults", { method: "PUT", body: {} }).catch((e) => e);
    expect(error.status).toBe(422);
    expect(error.fields).toEqual({
      minSeeders: "Input should be >= 0",
      body: "Extra inputs are not permitted",
    });
    expect(error.message).toMatch(/minSeeders/);
  });

  it("falls back to statusText when the error body is not JSON", async () => {
    mockFetch({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new Error("not json");
      },
    });
    const error = await request("/series").catch((e) => e);
    expect(error.status).toBe(502);
    expect(error.message).toBe("Bad Gateway");
  });

  it("passes an abort signal through to fetch", async () => {
    const fetchMock = mockFetch(jsonResponse(200, {}));
    const controller = new AbortController();
    await request("/series", { signal: controller.signal });
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });
});
