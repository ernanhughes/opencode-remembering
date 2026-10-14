/**
 * Best-effort secret scrubbing at persistence and memory-context boundaries.
 *
 * This is deliberately deterministic and local: never inspect process.env,
 * never log candidate values, and never store the input before sanitising it.
 * Named secrets in text, credential URLs, auth headers and sensitive structured
 * fields are covered. Unlabelled arbitrary secrets cannot be recognised.
 */
export const REDACTED_SECRET = "[REDACTED]";

export function isSensitiveField(name: string): boolean {
  const normal = name.replace(/[^a-z0-9]/gi, "").toLowerCase();
  if ([
    "authorization", "proxyauthorization", "cookie", "setcookie",
    "password", "passwd", "token", "secret", "credential", "credentials",
    "privatekey", "apikey", "clientsecret", "jwt",
  ].includes(normal)) return true;
  return /(?:apikey|authtoken|accesstoken|refreshtoken|idtoken|bearertoken|privatekey|clientsecret|secretaccesskey|password|passwd|credential|credentials|secret|token)$/.test(normal);
}

/** Redact known credential formats while preserving ordinary diagnostic text. */
export function redactSecrets(text: string): string {
  if (!text) return text;
  let result = text;

  // HTTP authorization headers, including basic auth and bearer tokens.
  result = result.replace(
    /\b(Authorization|Proxy-Authorization)([ \t]*[:=][ \t]*)(?!\[REDACTED\])[^\r\n]+/gi,
    (_full, name: string, separator: string) => name + separator + REDACTED_SECRET,
  );

  // Credentials in URLs (the username is useful for troubleshooting).
  result = result.replace(
    /\b([a-z][a-z0-9+.-]*:\/\/[^\/@\s:]+:)([^\/@\s]+)(@)/gi,
    (_full, prefix: string, _password: string, suffix: string) =>
      prefix + REDACTED_SECRET + suffix,
  );

  // URL query credentials, including names such as api_key and access_token.
  result = result.replace(
    /([?&]([a-z_][a-z0-9_-]*)=)([^&#\s]+)/gi,
    (full, prefix: string, name: string) =>
      isSensitiveField(name) ? prefix + REDACTED_SECRET : full,
  );

  // JSON and JS-like quoted object keys: "OPENCODE_API_KEY": "value".
  result = result.replace(
    /(["'])([A-Za-z_][\w.-]*)\1([ \t]*:[ \t]*)(["'])(.*?)\4/g,
    (full, keyQuote: string, name: string, separator: string, valueQuote: string) =>
      isSensitiveField(name)
        ? keyQuote + name + keyQuote + separator + valueQuote + REDACTED_SECRET + valueQuote
        : full,
  );

  // Shell environment assignments, env listing tables, and YAML/key-value logs.
  result = result.replace(
    /\b([A-Za-z_][\w.-]*)([ \t]*(?:=|:)[ \t]*|[ \t]{2,})("(?:\\.|[^"\r\n])*"|'(?:\\.|[^'\r\n])*'|[^\s&;,}\]\r\n]+)/g,
    (full, name: string, separator: string, value: string) => {
      if (!isSensitiveField(name) || value === REDACTED_SECRET) return full;
      const quote = value.startsWith('"') || value.startsWith("'") ? value[0] : "";
      return name + separator + (quote ? quote + REDACTED_SECRET + quote : REDACTED_SECRET);
    },
  );
  return result;
}

/**
 * Sanitize nested tool calls/results before serialisation, including values
 * whose field name (rather than value) identifies them as credentials.
 */
export function redactSensitive<T>(input: T): T {
  const seen = new WeakSet<object>();
  function walk(value: unknown, depth: number): unknown {
    if (typeof value === "string") return redactSecrets(value);
    if (value === null || typeof value !== "object") return value;
    if (depth >= 40 || seen.has(value)) return REDACTED_SECRET;
    seen.add(value);
    try {
      if (Array.isArray(value)) return value.map((entry) => walk(entry, depth + 1));
      return Object.fromEntries(
        Object.entries(value).map(([key, entry]) => [
          key,
          isSensitiveField(key) ? REDACTED_SECRET : walk(entry, depth + 1),
        ]),
      );
    } finally {
      seen.delete(value);
    }
  }
  return walk(input, 0) as T;
}
