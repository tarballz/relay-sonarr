// The single fetch wrapper over the backend's same-origin /api surface.
// Everything the UI knows about an HTTP failure comes from ApiError.

export class ApiError extends Error {
  constructor(message, { status, detail, fields } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status ?? 0;
    this.detail = detail ?? message;
    this.fields = fields ?? {};
  }
}

// FastAPI validation errors arrive as a list of {loc, msg}; flatten them to
// {field: message} so a form can show each message next to its own input.
function validationFields(detail) {
  const fields = {};
  for (const item of detail) {
    const loc = Array.isArray(item?.loc) ? item.loc : [];
    const name = loc.length > 1 ? String(loc[loc.length - 1]) : String(loc[0] ?? "body");
    if (item?.msg) fields[name] = item.msg;
  }
  return fields;
}

export async function request(path, { method = "GET", body, signal, headers } = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (signal) init.signal = signal;

  const res = await fetch(`/api${path}`, init);

  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      /* not a JSON body — fall back to statusText */
    }
    const detail = payload?.detail;
    if (Array.isArray(detail)) {
      const fields = validationFields(detail);
      const summary = Object.entries(fields)
        .map(([field, message]) => `${field}: ${message}`)
        .join("; ");
      throw new ApiError(summary || res.statusText, { status: res.status, detail, fields });
    }
    const message = typeof detail === "string" && detail ? detail : res.statusText;
    throw new ApiError(message, { status: res.status, detail: message });
  }

  // 204 (and any empty body) is a success with nothing to parse.
  if (res.status === 204) return null;
  return res.json();
}
