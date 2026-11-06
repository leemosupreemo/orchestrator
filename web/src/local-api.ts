const TOKEN_KEY = 'orchestrator-local-token';
let cachedToken: string | undefined;

// A network link carries the token once; keep it for this tab and remove it from the address bar.
function loadToken(): string {
  let token = '';
  try { token = sessionStorage.getItem(TOKEN_KEY) || ''; } catch {}
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get('token');
  if (fromUrl) {
    token = fromUrl;
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch {}
    url.searchParams.delete('token');
    window.history.replaceState(null, '', url.toString());
  }
  return token;
}

export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  cachedToken ??= loadToken();
  const headers = new Headers(init.headers);
  if (cachedToken) headers.set('Authorization', `Bearer ${cachedToken}`);
  return fetch(path, { ...init, headers });
}
