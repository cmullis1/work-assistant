/* A small stand-in for the browser DOM, so node tests can run the builders in app.js and charts.js and
   inspect the elements they make. It covers what those builders use: elements and text nodes, classes,
   attributes, children, textContent, form values, event listeners, and simple selectors (a tag, classes and
   [attribute] names, with descendant combinators, in a comma list) for querySelectorAll, querySelector, closest and
   getElementById. Layout is whatever the test says it is: set globalThis.layout to a function (element) ->
   { clientWidth, scrollWidth, offsetLeft, offsetHeight, rect: { left, right, width } }; an element
   without a rect is at 0, 0, and one without an offset has 0. */
"use strict";

class Node {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
  }

  get textContent() {
    return this.childNodes.map(function (c) { return c.textContent; }).join("");
  }

  set textContent(value) {
    this.childNodes.forEach(function (c) { c.parentNode = null; });
    this.childNodes = [];
    if (value != null && value !== "") {
      this.appendChild(new Text(String(value)));
    }
  }

  appendChild(child) {
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    this.childNodes = this.childNodes.filter(function (c) { return c !== child; });
    child.parentNode = null;
    return child;
  }

  /* Put `child` right before `ref`, one of this node's children, or last when `ref` is null. */
  insertBefore(child, ref) {
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    var at = ref ? this.childNodes.indexOf(ref) : -1;
    if (at < 0) {
      this.childNodes.push(child);
    } else {
      this.childNodes.splice(at, 0, child);
    }
    return child;
  }

  replaceChildren() {
    this.textContent = "";
    for (var i = 0; i < arguments.length; i++) {
      this.appendChild(arguments[i]);
    }
  }

  contains(node) {
    while (node) {
      if (node === this) {
        return true;
      }
      node = node.parentNode;
    }
    return false;
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  /* Put `nodes` first among this one's children, in order. */
  prepend() {
    var parent = this;
    var nodes = Array.prototype.slice.call(arguments);
    nodes.forEach(function (n) {
      if (n.parentNode) {
        n.parentNode.removeChild(n);
      }
      n.parentNode = parent;
    });
    this.childNodes = nodes.concat(this.childNodes);
  }

  /* Put `nodes` right after this one, in order. */
  after() {
    var parent = this.parentNode;
    var nodes = Array.prototype.slice.call(arguments);
    nodes.forEach(function (n) {
      if (n.parentNode) {
        n.parentNode.removeChild(n);
      }
      n.parentNode = parent;
    });
    var at = parent.childNodes.indexOf(this);
    parent.childNodes.splice.apply(parent.childNodes, [at + 1, 0].concat(nodes));
  }

  replaceWith() {
    var parent = this.parentNode;
    var nodes = Array.prototype.slice.call(arguments);
    nodes.forEach(function (n) {
      if (n.parentNode) {
        n.parentNode.removeChild(n);
      }
      n.parentNode = parent;
    });
    var at = parent.childNodes.indexOf(this);
    parent.childNodes.splice.apply(parent.childNodes, [at, 1].concat(nodes));
    this.parentNode = null;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get nextSibling() {
    var siblings = this.parentNode ? this.parentNode.childNodes : [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get isConnected() {
    var node = this;
    while (node.parentNode) {
      node = node.parentNode;
    }
    return node === globalThis.document;
  }
}

class Text extends Node {
  constructor(data) {
    super();
    this.data = data;
  }

  get nodeType() {
    return 3;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

/* Whether `el` matches one simple selector: an optional tag, any classes, any [attribute] names. */
function matchesSimple(el, simple) {
  var m = /^([a-zA-Z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+\])*)$/.exec(simple);
  if (!m) {
    throw new Error("fake_dom: unsupported selector " + simple);
  }
  if (m[1] && el.tagName !== m[1].toUpperCase()) {
    return false;
  }
  var classes = m[2].split(".").filter(Boolean);
  var attrs = m[3].match(/[\w-]+/g) || [];
  return classes.every(function (c) { return el.classList.contains(c); }) &&
    attrs.every(function (a) { return el.getAttribute(a) !== null; });
}

/* Whether `el` matches `selector`: simple selectors joined by descendant combinators (spaces), or a list of those
   (commas), matched when any one of them is. */
function matches(el, selector) {
  if (selector.indexOf(",") >= 0) {
    return selector.split(",").some(function (one) { return matches(el, one); });
  }
  var parts = selector.trim().split(/\s+/);
  if (!matchesSimple(el, parts[parts.length - 1])) {
    return false;
  }
  var i = parts.length - 2;
  for (var node = el.parentNode; i >= 0 && node instanceof Element; node = node.parentNode) {
    if (matchesSimple(node, parts[i])) {
      i -= 1;
    }
  }
  return i < 0;
}

class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.listeners = {};
    this.style = {};
  }

  get nodeType() {
    return 1;
  }

  get children() {
    return this.childNodes.filter(function (c) { return c instanceof Element; });
  }

  get className() {
    return this.attributes["class"] || "";
  }

  set className(value) {
    this.attributes["class"] = String(value);
  }

  get classList() {
    var el = this;
    var names = function () { return el.className.split(/\s+/).filter(Boolean); };
    return {
      contains: function (name) { return names().indexOf(name) >= 0; },
      add: function (name) { this.toggle(name, true); },
      remove: function (name) { this.toggle(name, false); },
      toggle: function (name, on) {
        var has = names().indexOf(name) >= 0;
        var want = on === undefined ? !has : Boolean(on);
        if (want && !has) {
          el.className = names().concat([name]).join(" ");
        } else if (!want && has) {
          el.className = names().filter(function (n) { return n !== name; }).join(" ");
        }
        return want;
      }
    };
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  /* As in a browser: what was typed or chosen, else the value attribute; a select's is its selected option's
     (the first option when none is marked selected), an option's its value attribute or else its text. */
  get value() {
    if (this._value !== undefined) {
      return this._value;
    }
    if (this.tagName === "SELECT") {
      var options = this.findAll(function (e) { return e.tagName === "OPTION"; });
      var chosen = options.filter(function (o) { return o.getAttribute("selected") !== null; })[0] || options[0];
      return chosen ? chosen.value : "";
    }
    var attr = this.getAttribute("value");
    if (attr === null) {
      return this.tagName === "OPTION" ? this.textContent : "";
    }
    return attr;
  }

  set value(value) {
    this._value = String(value);
  }

  querySelectorAll(selector) {
    return this.findAll(function (e) { return matches(e, selector); });
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    for (var node = this; node instanceof Element; node = node.parentNode) {
      if (matches(node, selector)) {
        return node;
      }
    }
    return null;
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  focus() {
    globalThis.focused = this;
  }

  get clientWidth() {
    return globalThis.layout ? globalThis.layout(this).clientWidth : 0;
  }

  get scrollWidth() {
    return globalThis.layout ? globalThis.layout(this).scrollWidth : 0;
  }

  get offsetLeft() {
    return (globalThis.layout && globalThis.layout(this).offsetLeft) || 0;
  }

  get offsetHeight() {
    return (globalThis.layout && globalThis.layout(this).offsetHeight) || 0;
  }

  get offsetWidth() {
    return (globalThis.layout && globalThis.layout(this).offsetWidth) || 0;
  }

  get previousElementSibling() {
    var siblings = this.parentNode ? this.parentNode.children : [];
    return siblings[siblings.indexOf(this) - 1] || null;
  }

  getBoundingClientRect() {
    var rect = globalThis.layout ? globalThis.layout(this).rect : null;
    return Object.assign({ left: 0, right: 0, width: 0, top: 0, bottom: 0, height: 0 }, rect);
  }

  /* Every descendant element that `test(element)` accepts, in document order. */
  findAll(test) {
    var found = [];
    this.children.forEach(function (c) {
      if (test(c)) {
        found.push(c);
      }
      found = found.concat(c.findAll(test));
    });
    return found;
  }
}

class Document extends Node {
  constructor() {
    super();
    this.listeners = {};
    this.documentElement = new Element("html");
    this.body = new Element("body");
    this.appendChild(this.documentElement);
    this.documentElement.appendChild(this.body);
  }

  createElement(tag) {
    return new Element(tag);
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  createElementNS(_namespace, tag) {
    return new Element(tag);
  }

  createTextNode(data) {
    return new Text(String(data));
  }

  querySelectorAll(selector) {
    return [this.documentElement].concat(this.documentElement.findAll(function () { return true; }))
      .filter(function (e) { return matches(e, selector); });
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  getElementById(id) {
    return this.documentElement.findAll(function (e) { return e.getAttribute("id") === id; })[0] || null;
  }
}

globalThis.Node = Node;
globalThis.document = new Document();
globalThis.window = globalThis;
