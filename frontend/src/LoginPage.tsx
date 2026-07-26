import { useState } from "react";
import { api } from "./api";

export function LoginPage({ onAuthenticated }: { onAuthenticated: (username: string) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const session = await api.login(username, password, totpCode);
      onAuthenticated(session.username);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  return <main className="login-shell">
    <section className="login-card">
      <div className="brand login-brand"><div className="mark"><span /></div><div><strong>BASIS HAWK</strong><small>SECURE OPERATOR CONSOLE</small></div></div>
      <p className="eyebrow">ADMIN AUTHENTICATION</p>
      <h1>登录控制台</h1>
      <p>使用管理员密码和身份验证器中的动态验证码。</p>
      <form onSubmit={submit}>
        <label>用户名<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        <label>动态验证码<input inputMode="numeric" autoComplete="one-time-code" value={totpCode} onChange={(event) => setTotpCode(event.target.value)} minLength={6} maxLength={6} required /></label>
        {error && <div className="error-banner">{error}</div>}
        <button className="button primary" disabled={submitting}>{submitting ? "正在验证…" : "登录"}</button>
      </form>
    </section>
  </main>;
}
