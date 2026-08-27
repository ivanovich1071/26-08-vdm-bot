/**
 * Виджет-консультант для сайта ЭЛТИ-КУДИЦ.
 *
 * Подключается одним тегом и не требует сборки:
 *   <script src="https://<хост>/widget.js" defer></script>
 *
 * Витрины здесь нет: подобранные позиции показываются строками, а корзина — списком
 * заявки. Вся логика продажи живёт на сервере, виджет только рисует то, что пришло.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var BASE = (script && script.dataset.base) || new URL(script.src).origin;
  var STORAGE_KEY = "vdm_widget_session";

  var state = { sessionId: null, open: false, busy: false };

  // --- Разметка ------------------------------------------------------------

  var css = [
    ".vdm-launcher{position:fixed;right:20px;bottom:20px;z-index:99998;width:60px;height:60px;",
    "border-radius:50%;border:0;cursor:pointer;background:#0b6ab0;color:#fff;font-size:26px;",
    "box-shadow:0 6px 20px rgba(0,0,0,.25)}",
    ".vdm-panel{position:fixed;right:20px;bottom:92px;z-index:99999;width:380px;max-width:calc(100vw - 32px);",
    "height:560px;max-height:calc(100vh - 120px);display:none;flex-direction:column;background:#fff;",
    "border-radius:14px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.28);",
    "font:14px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1c2430}",
    ".vdm-panel.vdm-open{display:flex}",
    ".vdm-head{background:#0b6ab0;color:#fff;padding:12px 16px;font-weight:600}",
    ".vdm-head small{display:block;font-weight:400;opacity:.85;font-size:12px}",
    ".vdm-log{flex:1;overflow-y:auto;padding:14px;background:#f5f7fa}",
    ".vdm-msg{margin-bottom:12px;padding:10px 12px;border-radius:10px;background:#fff;",
    "box-shadow:0 1px 3px rgba(0,0,0,.08);white-space:pre-wrap;word-wrap:break-word}",
    ".vdm-msg.vdm-me{background:#0b6ab0;color:#fff;margin-left:44px}",
    ".vdm-item{border-top:1px solid #eaeef3;padding:8px 0}",
    ".vdm-item:first-child{border-top:0}",
    ".vdm-item b{display:block}",
    ".vdm-meta{color:#5a6a7d;font-size:13px}",
    ".vdm-norm{color:#0b6ab0;font-size:12px;margin-top:2px}",
    ".vdm-acts{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}",
    ".vdm-btn{border:1px solid #0b6ab0;background:#fff;color:#0b6ab0;border-radius:16px;",
    "padding:5px 12px;font-size:13px;cursor:pointer}",
    ".vdm-btn:hover{background:#0b6ab0;color:#fff}",
    ".vdm-form{display:flex;border-top:1px solid #e3e8ee;background:#fff}",
    ".vdm-form input{flex:1;border:0;padding:14px;font-size:14px;outline:none}",
    ".vdm-form button{border:0;background:#0b6ab0;color:#fff;padding:0 18px;cursor:pointer}",
    ".vdm-typing{color:#7d8b9c;font-style:italic}",
    ".vdm-total{margin-top:8px;font-weight:600}",
  ].join("");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  var style = el("style");
  style.textContent = css;
  document.head.appendChild(style);

  var launcher = el("button", "vdm-launcher", "💬");
  launcher.setAttribute("aria-label", "Открыть чат подбора оборудования");

  var panel = el("div", "vdm-panel");
  var head = el("div", "vdm-head", "Подбор оборудования");
  head.appendChild(el("small", null, "ЭЛТИ-КУДИЦ · консультант"));
  var log = el("div", "vdm-log");
  var form = el("form", "vdm-form");
  var input = el("input");
  input.placeholder = "Что нужно подобрать?";
  input.autocomplete = "off";
  var submit = el("button", null, "→");
  submit.type = "submit";
  form.appendChild(input);
  form.appendChild(submit);
  panel.appendChild(head);
  panel.appendChild(log);
  panel.appendChild(form);

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  // --- Сеть ----------------------------------------------------------------

  function post(path, body) {
    return fetch(BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function ensureSession() {
    if (state.sessionId) return Promise.resolve(state.sessionId);
    // Идентификатор анонимный и живёт в браузере посетителя: до согласия
    // никаких персональных данных не собираем и на сервер не отправляем.
    var saved = null;
    try {
      saved = localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      saved = null;
    }
    // Даже с сохранённым идентификатором обращаемся к серверу: он вернёт
    // приветствие и текущую корзину, иначе окно откроется пустым.
    return post("/widget/session", saved ? { session_id: saved } : {}).then(function (data) {
      state.sessionId = data.session_id;
      try {
        localStorage.setItem(STORAGE_KEY, data.session_id);
      } catch (e) {
        /* приватный режим — просто не запоминаем */
      }
      render(data.responses);
      return data.session_id;
    });
  }

  // --- Отрисовка -----------------------------------------------------------

  function actionsNode(rows) {
    if (!rows || !rows.length) return null;
    var box = el("div", "vdm-acts");
    rows.forEach(function (row) {
      row.forEach(function (button) {
        var node = el("button", "vdm-btn", button.title);
        node.type = "button";
        node.onclick = function () {
          if (button.url) {
            window.open(button.url, "_blank", "noopener");
          } else {
            sendAction(button.action);
          }
        };
        box.appendChild(node);
      });
    });
    return box;
  }

  function itemNode(item) {
    var node = el("div", "vdm-item");
    node.appendChild(el("b", null, item.text));
    if (item.meta) node.appendChild(el("div", "vdm-meta", item.meta));
    if (item.norm) node.appendChild(el("div", "vdm-norm", item.norm));
    var acts = actionsNode(item.actions);
    if (acts) node.appendChild(acts);
    return node;
  }

  function render(responses) {
    (responses || []).forEach(function (response) {
      var box = el("div", "vdm-msg");
      if (response.type === "text") {
        box.textContent = response.text;
      } else if (response.type === "item") {
        box.appendChild(itemNode(response));
      } else if (response.type === "list") {
        box.appendChild(el("b", null, response.title));
        response.items.forEach(function (item) {
          box.appendChild(itemNode(item));
        });
      } else if (response.type === "order") {
        box.appendChild(el("b", null, "Ваш заказ"));
        response.lines.forEach(function (line) {
          box.appendChild(
            el("div", "vdm-meta", line.quantity + " × " + line.name + " — " + line.price)
          );
        });
        box.appendChild(el("div", "vdm-total", "Итого: " + response.total));
        if (response.note) box.appendChild(el("div", "vdm-meta", response.note));
      }
      var acts = actionsNode(response.actions);
      if (acts) box.appendChild(acts);
      log.appendChild(box);
    });
    log.scrollTop = log.scrollHeight;
  }

  function typing(on) {
    var existing = log.querySelector(".vdm-typing");
    if (on && !existing) {
      var node = el("div", "vdm-msg vdm-typing", "Подбираю…");
      log.appendChild(node);
      log.scrollTop = log.scrollHeight;
    } else if (!on && existing) {
      existing.remove();
    }
  }

  // --- Действия ------------------------------------------------------------

  function call(path, payload) {
    if (state.busy) return;
    state.busy = true;
    typing(true);
    ensureSession()
      .then(function (sessionId) {
        payload.session_id = sessionId;
        return post(path, payload);
      })
      .then(function (data) {
        render(data.responses);
      })
      .catch(function (error) {
        render([{ type: "text", text: "Связь прервалась: " + error.message }]);
      })
      .finally(function () {
        typing(false);
        state.busy = false;
      });
  }

  function sendAction(action) {
    call("/widget/action", { action: action });
  }

  form.onsubmit = function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    render([{ type: "text", text: text }]);
    log.lastChild.classList.add("vdm-me");
    call("/widget/message", { text: text });
  };

  launcher.onclick = function () {
    state.open = !state.open;
    panel.classList.toggle("vdm-open", state.open);
    if (state.open) {
      ensureSession();
      input.focus();
    }
  };
})();
