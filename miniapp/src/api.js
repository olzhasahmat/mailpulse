// initData подписан токеном бота — сервер по нему опознаёт пользователя без ввода аккаунта.
// Для локальной разработки вне Telegram можно передать ?initData=... в URL.
const initData =
  window.Telegram?.WebApp?.initData ||
  new URLSearchParams(location.search).get("initData") ||
  "";

async function request(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: {
      Authorization: `tma ${initData}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `Ошибка ${resp.status}`);
  return data;
}

export function openLink(url) {
  const tg = window.Telegram?.WebApp;
  if (tg?.openLink) tg.openLink(url);
  else window.open(url, "_blank");
}

export const api = {
  hasAuth: () => Boolean(initData),
  accounts: () => request("GET", "/api/accounts"),
  connectAccount: (payload) => request("POST", "/api/accounts", payload),
  disconnectAccount: (id) => request("DELETE", `/api/accounts/${id}`),
  rules: () => request("GET", "/api/rules"),
  addRule: (payload) => request("POST", "/api/rules", payload),
  deleteRule: (id) => request("DELETE", `/api/rules/${id}`),
  stats: () => request("GET", "/api/stats"),
};
