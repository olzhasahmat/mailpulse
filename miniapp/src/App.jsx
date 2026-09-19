import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const IMPORTANCE = { 3: "🔴 Срочно", 2: "🟡 Важно", 1: "⚪️ К сведению", 0: "Шум" };

export default function App() {
  const [tab, setTab] = useState("stats");
  if (!api.hasAuth()) {
    return (
      <div className="app">
        <h1>MailPulse</h1>
        <p className="center">Откройте приложение из Telegram-бота @mailpulse_olzhas_bot.</p>
      </div>
    );
  }
  return (
    <div className="app">
      <h1>MailPulse</h1>
      {tab === "stats" && <Stats />}
      {tab === "accounts" && <Accounts />}
      {tab === "rules" && <Rules />}
      <nav className="tabs">
        <button className={tab === "stats" ? "active" : ""} onClick={() => setTab("stats")}>📊 Статистика</button>
        <button className={tab === "accounts" ? "active" : ""} onClick={() => setTab("accounts")}>📮 Ящики</button>
        <button className={tab === "rules" ? "active" : ""} onClick={() => setTab("rules")}>⚙️ Правила</button>
      </nav>
    </div>
  );
}

function useAsync(loader, deps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const reload = () => {
    setState((s) => ({ ...s, loading: true }));
    loader().then(
      (data) => setState({ loading: false, data, error: null }),
      (error) => setState({ loading: false, data: null, error: error.message })
    );
  };
  useEffect(reload, deps);
  return { ...state, reload };
}

function Stats() {
  const { loading, data, error } = useAsync(() => api.stats());
  if (loading) return <p className="center">Загрузка…</p>;
  if (error) return <p className="error">{error}</p>;
  return (
    <>
      <div className="grid">
        <Stat n={data.accounts} l="ящиков" />
        <Stat n={data.messages} l="писем" />
        <Stat n={data.processed} l="разобрано" />
        <Stat n={`$${data.total_cost_usd}`} l="затрачено" />
      </div>
      <h2>По важности</h2>
      {data.by_importance.map((b) => (
        <div className="card row" key={b.importance}>
          <span>{IMPORTANCE[b.importance]}</span>
          <span className="badge">{b.count}</span>
        </div>
      ))}
      <h2>Важное недавно</h2>
      {data.recent.length === 0 && <p className="muted">Пока пусто.</p>}
      {data.recent.map((e) => (
        <div className="card" key={e.message_id}>
          <div className="row">
            <b className={`imp${e.importance}`}>{IMPORTANCE[e.importance]}</b>
            {e.needs_reply && <span className="badge">ждёт ответа</span>}
          </div>
          <div>{e.subject || "(без темы)"}</div>
          <div className="muted">{e.from_addr}</div>
        </div>
      ))}
    </>
  );
}

const Stat = ({ n, l }) => (
  <div className="stat"><div className="n">{n}</div><div className="l">{l}</div></div>
);

function Accounts() {
  const { loading, data, error, reload } = useAsync(() => api.accounts());
  const [form, setForm] = useState({ email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);

  const connect = async () => {
    setBusy(true);
    setFormError(null);
    try {
      await api.connectAccount(form);
      setForm({ email: "", password: "" });
      reload();
    } catch (e) {
      setFormError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h2>Подключить ящик</h2>
      <div className="card">
        <input placeholder="you@gmail.com" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input placeholder="Пароль приложения" type="password" value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
        {formError && <div className="error">{formError}</div>}
        <button onClick={connect} disabled={busy || !form.email || !form.password}>
          {busy ? "Проверяю…" : "Подключить"}
        </button>
        <p className="muted">Gmail и Яндекс — по паролю приложения, не по обычному паролю.</p>
      </div>
      <h2>Мои ящики</h2>
      {loading && <p className="center">Загрузка…</p>}
      {error && <p className="error">{error}</p>}
      {data?.length === 0 && <p className="muted">Ящиков пока нет.</p>}
      {data?.map((a) => (
        <div className="card row" key={a.id}>
          <div>
            <div>{a.email}</div>
            <div className="muted">{a.provider} · {a.status}{a.last_error ? ` · ${a.last_error}` : ""}</div>
          </div>
          <button className="ghost" onClick={() => api.disconnectAccount(a.id).then(reload)}>Отключить</button>
        </div>
      ))}
    </>
  );
}

function Rules() {
  const { loading, data, error, reload } = useAsync(() => api.rules());
  const [form, setForm] = useState({ kind: "vip", value: "" });

  const add = async () => {
    if (!form.value) return;
    await api.addRule(form);
    setForm({ ...form, value: "" });
    reload();
  };

  return (
    <>
      <h2>Добавить правило</h2>
      <div className="card">
        <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="vip">VIP — всегда важно</option>
          <option value="mute">Заглушить — не уведомлять</option>
        </select>
        <input placeholder="адрес или @домен" value={form.value}
          onChange={(e) => setForm({ ...form, value: e.target.value })} />
        <button onClick={add} disabled={!form.value}>Добавить</button>
      </div>
      <h2>Мои правила</h2>
      {loading && <p className="center">Загрузка…</p>}
      {error && <p className="error">{error}</p>}
      {data?.length === 0 && <p className="muted">Правил пока нет.</p>}
      {data?.map((r) => (
        <div className="card row" key={r.id}>
          <div>
            <span className="badge">{r.kind === "vip" ? "VIP" : "mute"}</span> {r.value}
          </div>
          <button className="ghost" onClick={() => api.deleteRule(r.id).then(reload)}>Удалить</button>
        </div>
      ))}
    </>
  );
}
