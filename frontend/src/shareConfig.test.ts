import { describe, expect, it } from "vitest";
import config from "../vite.config";

describe("vite config for sharing through a tunnel", () => {
  it("allows tunnel host names for the dev server and for preview", () => {
    expect(config.server?.allowedHosts).toContain(".trycloudflare.com");
    expect(config.preview?.allowedHosts).toContain(".trycloudflare.com");
  });

  it("never switches host checking off, and keeps the normal local ports and hosts", () => {
    // an allow-list (not `true`) keeps localhost and 127.0.0.1 working and everything else out
    expect(Array.isArray(config.server?.allowedHosts)).toBe(true);
    expect(Array.isArray(config.preview?.allowedHosts)).toBe(true);
    expect(config.server?.port).toBe(5173);
    expect(config.preview?.host).toBe("127.0.0.1");
    expect(config.preview?.port).toBe(4173);
  });

  it("proxies /api to the backend the same way for preview as for the dev server", () => {
    expect(config.server?.proxy).toEqual({ "/api": "http://127.0.0.1:8000" });
    expect(config.preview?.proxy).toEqual(config.server?.proxy);
  });
});

// every front end source file as text (test files excluded), via Vite's own glob import
const raw = import.meta.glob(["./**/*.{ts,tsx,css}", "../index.html"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const sources = Object.entries(raw).filter(([path]) => !/\.test\.tsx?$/.test(path));

describe("the front end only talks to the backend through relative /api paths", () => {
  it("reads the source files it is supposed to check", () => {
    expect(sources.length).toBeGreaterThan(10);
    expect(sources.some(([p]) => p.endsWith("api.ts"))).toBe(true);
    expect(sources.some(([p]) => p.endsWith("index.html"))).toBe(true);
  });

  it("has no hard-coded localhost or 127.0.0.1 address in any source file", () => {
    const offenders = sources.filter(([, text]) => /(localhost|127\.0\.0\.1)\s*:/.test(text)).map(([p]) => p);
    expect(offenders).toEqual([]);
  });

  it("calls the API only with relative /api paths", () => {
    for (const [path, text] of sources) {
      expect(text, path).not.toMatch(/fetch\(\s*["'`]https?:\/\//);
      expect(text, path).not.toMatch(/["'`]https?:\/\/[^"'`]*\/api\//);
    }
  });
});
