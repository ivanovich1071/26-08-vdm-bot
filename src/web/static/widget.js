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
    ".vdm-acts{margin-top:8px;display:flex;flex-direction:column;gap:6px}",
    ".vdm-acts-row{display:flex;flex-wrap:wrap;gap:6px}",
    ".vdm-line{margin-top:6px}",
    ".vdm-btn{border:1px solid #0b6ab0;background:#fff;color:#0b6ab0;border-radius:16px;",
    "padding:5px 12px;font-size:13px;cursor:pointer}",
    ".vdm-btn:hover{background:#0b6ab0;color:#fff}",
    ".vdm-btn-flat{border-color:#d6dee7;color:#5a6a7d;cursor:default}",
    ".vdm-btn-flat:hover{background:#fff;color:#5a6a7d}",
    ".vdm-form{display:flex;border-top:1px solid #e3e8ee;background:#fff}",
    ".vdm-form input{flex:1;border:0;padding:14px;font-size:14px;outline:none}",
    ".vdm-form button{border:0;background:#0b6ab0;color:#fff;padding:0 18px;cursor:pointer}",
    ".vdm-typing{color:#7d8b9c;font-style:italic}",
    ".vdm-total{margin-top:8px;font-weight:600}",
    ".vdm-photo{display:block;width:100%;max-width:220px;border-radius:8px;margin:6px 0}",
    ".vdm-props{margin-top:6px;font-size:13px;color:#3d4b5c}",
    ".vdm-props div{display:flex;gap:6px}",
    ".vdm-props span:first-child{color:#7d8b9c}",
    ".vdm-desc{margin-top:6px;font-size:13px;white-space:pre-wrap}",
    ".vdm-kit{margin:6px 0 0;padding-left:18px;font-size:13px}",
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

  // Ряд кнопок — свой контейнер. Раньше все кнопки складывались в один, и
  // разбиение на ряды терялось: «−», «1 шт.», «+» и «Удалить» от разных товаров
  // перемешивались в одну ленту, и понять, что к чему относится, было нельзя.
  function actionsNode(rows) {
    if (!rows || !rows.length) return null;
    var box = el("div", "vdm-acts");
    rows.forEach(function (row) {
      var line = el("div", "vdm-acts-row");
      row.forEach(function (button) {
        var node = el("button", "vdm-btn", button.title);
        node.type = "button";
        // Надпись с количеством кнопкой только выглядит: нажимать её незачем.
        if (button.action === "noop") {
          node.disabled = true;
          node.className = "vdm-btn vdm-btn-flat";
        } else {
          node.onclick = function () {
            if (button.url) {
              window.open(button.url, "_blank", "noopener");
            } else {
              sendAction(button.action);
            }
          };
        }
        line.appendChild(node);
      });
      box.appendChild(line);
    });
    return box;
  }

  // Подробная карточка приходит с фотографией, характеристиками и составом;
  // строка в списке выдачи — без них. Отличаем по наличию полей, а не по типу:
  // ядро отдаёт и то и другое как "item".
  function itemNode(item) {
    var node = el("div", "vdm-item");
    if (item.image) {
      var photo = el("img", "vdm-photo");
      photo.src = item.image;
      photo.alt = item.text;
      photo.loading = "lazy";
      // Снимок мог не собраться — тогда убираем картинку, а не показываем
      // сломанный значок поверх карточки.
      photo.onerror = function () {
        photo.remove();
      };
      node.appendChild(photo);
    }
    node.appendChild(el("b", null, item.text));
    if (item.meta) node.appendChild(el("div", "vdm-meta", item.meta));
    // В подробной карточке — все основания с формулировками пунктов приказа,
    // в строке выдачи — одно, ближайшее к запросу.
    if (item.norms && item.norms.length) {
      item.norms.forEach(function (line) {
        node.appendChild(el("div", "vdm-norm", line));
      });
    } else if (item.norm) {
      node.appendChild(el("div", "vdm-norm", item.norm));
    }

    var names = Object.keys(item.attributes || {});
    if (names.length) {
      var props = el("div", "vdm-props");
      names.forEach(function (name) {
        if (name.toLowerCase() === "код") return;
        var row = el("div");
        row.appendChild(el("span", null, name + ":"));
        row.appendChild(el("span", null, item.attributes[name]));
        props.appendChild(row);
      });
      if (props.childNodes.length) node.appendChild(props);
    }

    if (item.description) node.appendChild(el("div", "vdm-desc", item.description));
    if (item.kit && item.kit.length) {
      var list = el("ul", "vdm-kit");
      item.kit.forEach(function (line) {
        list.appendChild(el("li", null, line));
      });
      node.appendChild(list);
    }

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
        // Номер строки совпадает с номером на кнопках: иначе непонятно, какому
        // товару принадлежит «2 −».
        response.lines.forEach(function (line, index) {
          box.appendChild(el("div", "vdm-line", index + 1 + ". " + line.name));
          var tail = "    " + line.quantity + " × " + line.price;
          if (line.sku) tail += " · код 1С " + line.sku;
          box.appendChild(el("div", "vdm-meta", tail));
          if (line.norm) box.appendChild(el("div", "vdm-norm", "    " + line.norm));
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
