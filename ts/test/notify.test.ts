import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { notify, notifyDigest } from "../src/index.js";

const ORIG = { ...process.env };

function setCreds() {
  process.env.KAI_NOTIFY_BOT_TOKEN = "123:abc";
  process.env.KAI_NOTIFY_CHAT_ID = "660156312";
}

function mockFetch(ok = true) {
  const fn = vi.fn(async () => ({ ok, status: ok ? 200 : 500 }) as Response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

afterEach(() => {
  process.env = { ...ORIG };
});

describe("notify", () => {
  it("sends text and posts to the right url/body", async () => {
    setCreds();
    const fetchMock = mockFetch();
    expect(await notify("hello")).toBe(true);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("bot123:abc/sendMessage");
    const body = JSON.parse(opts.body as string);
    expect(body.text).toBe("hello");
    expect(body.chat_id).toBe("660156312");
  });

  it("trims and skips empty without sending", async () => {
    setCreds();
    const fetchMock = mockFetch();
    expect(await notify("  spaced  ")).toBe(true);
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.text).toBe("spaced");
    fetchMock.mockClear();
    expect(await notify("   ")).toBe(false);
    expect(await notify("")).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("notifyDigest", () => {
  it("formats bullets and drops blanks", async () => {
    setCreds();
    const fetchMock = mockFetch();
    expect(await notifyDigest("Today", ["a", "b", "  ", "c"])).toBe(true);
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.text).toBe("Today\n• a\n• b\n• c");
  });

  it("skips when nothing to say", async () => {
    setCreds();
    const fetchMock = mockFetch();
    expect(await notifyDigest("", [])).toBe(false);
    expect(await notifyDigest("", ["  ", ""])).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("fail-soft", () => {
  it("skips and never sends when credentials missing", async () => {
    delete process.env.KAI_NOTIFY_BOT_TOKEN;
    delete process.env.KAI_NOTIFY_CHAT_ID;
    const fetchMock = mockFetch();
    expect(await notify("hi")).toBe(false);
    expect(await notifyDigest("t", ["x"])).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("swallows a thrown fetch error", async () => {
    setCreds();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("telegram down");
      }),
    );
    expect(await notify("hi")).toBe(false);
  });

  it("returns false on non-ok HTTP", async () => {
    setCreds();
    mockFetch(false);
    expect(await notify("hi")).toBe(false);
  });
});
