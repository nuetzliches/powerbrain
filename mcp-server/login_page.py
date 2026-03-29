"""OAuth login page for Powerbrain MCP Server."""

import html


def render_login_page(
    callback_url: str,
    login_session_id: str,
    error: str | None = None,
) -> str:
    error_html = (
        f'<div class="error">{html.escape(error)}</div>' if error else ""
    )

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Powerbrain MCP – Anmelden</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      color: #333;
    }}
    .card {{
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.1);
      padding: 2rem;
      width: 100%;
      max-width: 400px;
    }}
    h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
    .subtitle {{
      color: #666;
      font-size: 0.875rem;
      margin-bottom: 1.5rem;
    }}
    .error {{
      background: #fef2f2;
      border: 1px solid #fca5a5;
      color: #b91c1c;
      padding: 0.75rem;
      border-radius: 6px;
      margin-bottom: 1rem;
      font-size: 0.875rem;
    }}
    label {{
      display: block;
      font-size: 0.875rem;
      font-weight: 500;
      margin-bottom: 0.25rem;
    }}
    input[type="text"] {{
      width: 100%;
      padding: 0.5rem 0.75rem;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      font-size: 0.875rem;
      font-family: monospace;
      margin-bottom: 1rem;
    }}
    input:focus {{
      outline: none;
      border-color: #2563eb;
      box-shadow: 0 0 0 2px rgba(37,99,235,0.2);
    }}
    button {{
      width: 100%;
      padding: 0.625rem;
      background: #2563eb;
      color: #fff;
      border: none;
      border-radius: 6px;
      font-size: 0.875rem;
      font-weight: 500;
      cursor: pointer;
    }}
    button:hover {{ background: #1d4ed8; }}
    .hint {{
      color: #999;
      font-size: 0.75rem;
      margin-top: 0.5rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Powerbrain MCP</h1>
    <p class="subtitle">Gib deinen Powerbrain API-Key ein, um fortzufahren.</p>
    {error_html}
    <form method="POST" action="{html.escape(callback_url)}">
      <input type="hidden" name="login_session_id" value="{html.escape(login_session_id)}">

      <label for="api_key">API-Key</label>
      <input type="text" id="api_key" name="api_key" required
             placeholder="pb_..." autocomplete="off">

      <button type="submit">Verbinden</button>
      <p class="hint">Der Key wird serverseitig validiert und mit deiner OAuth-Session verknüpft.</p>
    </form>
  </div>
</body>
</html>"""
