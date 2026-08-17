// app/web/static/js/core.js
//
// 모든 화면이 공유하는 유틸리티. 빌드 도구 없이 그대로 로드되므로 전역 객체 window.TG 에
// 필요한 것만 노출한다. 외부 라이브러리를 쓰지 않는다.
//
// XSS 주의: 이 서비스가 다루는 데이터는 공격자가 직접 입력한 닉네임과 본문이다.
// 실제로 닉네임에 <script> 문자열이 들어 있으므로, 서버에서 온 값은 반드시
// textContent 또는 escapeHtml 을 거쳐야 한다. innerHTML 에 직접 넣는 경로를 만들지 않는다.

(function (global) {
  'use strict';

  // ==========================================================================
  // 오류 타입
  // ==========================================================================

  /** API 호출 실패를 나타내는 오류. status 와 서버가 준 payload 를 함께 담는다. */
  function ApiError(message, status, payload) {
    var err = new Error(message);
    err.name = 'ApiError';
    err.status = status;
    err.payload = payload || null;
    return err;
  }

  // ==========================================================================
  // 문자열 / 숫자 / 시각 포맷터
  // ==========================================================================

  var HTML_ESCAPES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  };

  /** HTML 특수문자를 이스케이프한다. innerHTML 을 쓸 수밖에 없는 곳에서만 사용한다. */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
      return HTML_ESCAPES[ch];
    });
  }

  /** 3자리 구분 기호를 넣은 숫자 문자열. 숫자가 아니면 '0'. */
  function formatNumber(value) {
    var num = Number(value);
    if (!isFinite(num)) return '0';
    return num.toLocaleString('ko-KR');
  }

  /**
   * ISO 8601 문자열을 'YYYY-MM-DD HH:MM' 으로 바꾼다.
   * 서버가 이미 사용자 시간대(기본 KST) 기준으로 오프셋을 붙여 보내므로
   * Date 로 변환해 브라우저 시간대로 다시 옮기지 않고 문자열을 그대로 읽는다.
   */
  function formatDateTime(iso, options) {
    if (!iso) return '-';
    var opts = options || {};
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(iso));
    if (m) {
      var base = m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
      if (opts.seconds && m[6]) base += ':' + m[6];
      return base;
    }
    // 'YYYY-MM-DD HH:MM' 형태의 히스토그램 버킷처럼 오프셋이 없는 값도 그대로 살린다.
    var parsed = new Date(iso);
    if (isNaN(parsed.getTime())) return String(iso);
    return pad(parsed.getFullYear(), 4) + '-' + pad(parsed.getMonth() + 1) + '-' +
      pad(parsed.getDate()) + ' ' + pad(parsed.getHours()) + ':' + pad(parsed.getMinutes());
  }

  /** ISO 문자열에서 날짜 부분만 뽑는다. */
  function formatDate(iso) {
    if (!iso) return '-';
    var text = formatDateTime(iso);
    return text.length >= 10 ? text.slice(0, 10) : text;
  }

  /** 바이트 크기를 사람이 읽는 단위로 바꾼다. */
  function formatBytes(bytes) {
    var num = Number(bytes);
    if (!isFinite(num) || num < 0) return '-';
    if (num < 1024) return num + ' B';
    var units = ['KB', 'MB', 'GB'];
    var value = num / 1024;
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (value >= 10 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[index];
  }

  /** 0~100 백분율을 소수 한 자리로 자른다. */
  function formatPercent(value) {
    var num = Number(value);
    if (!isFinite(num)) return '0%';
    return (Math.round(num * 10) / 10) + '%';
  }

  function pad(value, width) {
    var text = String(value);
    var size = width || 2;
    while (text.length < size) text = '0' + text;
    return text;
  }

  /** 경로에서 파일 이름만 뽑는다. 백업 다운로드 링크 생성에 쓴다. */
  function baseName(path) {
    if (!path) return '';
    var parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || '';
  }

  /** 지정 길이를 넘으면 뒤를 자르고 말줄임을 붙인다. */
  function truncate(text, limit) {
    var value = String(text === null || text === undefined ? '' : text);
    var max = limit || 60;
    return value.length > max ? value.slice(0, max) + '...' : value;
  }

  // ==========================================================================
  // DOM 헬퍼
  // ==========================================================================

  function qs(selector, root) {
    return (root || document).querySelector(selector);
  }

  function qsa(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  /**
   * 요소를 만든다.
   * spec: { className, text, title, attrs, dataset, on, children }
   * text 는 항상 textContent 로 들어가므로 사용자 입력을 그대로 넘겨도 안전하다.
   */
  function el(tag, spec, children) {
    var node = document.createElement(tag);
    var config = spec || {};

    if (config.className) node.className = config.className;
    if (config.text !== undefined && config.text !== null) node.textContent = String(config.text);
    if (config.title !== undefined && config.title !== null) node.title = String(config.title);

    if (config.attrs) {
      Object.keys(config.attrs).forEach(function (name) {
        var value = config.attrs[name];
        if (value === null || value === undefined || value === false) return;
        node.setAttribute(name, value === true ? '' : String(value));
      });
    }
    if (config.dataset) {
      Object.keys(config.dataset).forEach(function (name) {
        node.dataset[name] = String(config.dataset[name]);
      });
    }
    if (config.on) {
      Object.keys(config.on).forEach(function (evt) {
        node.addEventListener(evt, config.on[evt]);
      });
    }

    appendChildren(node, config.children);
    appendChildren(node, children);
    return node;
  }

  function appendChildren(node, children) {
    if (children === null || children === undefined) return;
    var list = Array.isArray(children) ? children : [children];
    list.forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
  }

  /** 자식 노드를 모두 지운다. innerHTML = '' 대신 사용해 리스너 누수를 줄인다. */
  function clear(node) {
    if (!node) return node;
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';

  /** 인라인 SVG 아이콘을 만든다. d 는 코드에 고정된 경로 문자열이며 사용자 입력이 아니다. */
  function svgIcon(paths, size) {
    var box = size || 14;
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', String(box));
    svg.setAttribute('height', String(box));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    (Array.isArray(paths) ? paths : [paths]).forEach(function (d) {
      var path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function closeIcon() {
    return svgIcon(['M4 4l8 8', 'M12 4l-8 8'], 12);
  }

  /** 상태 배지 요소를 만든다. tone: green | blue | amber | red | gray */
  function badge(text, tone, title) {
    return el('span', {
      className: 'app-badge app-badge--' + (tone || 'gray'),
      text: text,
      title: title || null
    });
  }

  /** 빈 상태 카드 내용을 만든다. */
  function emptyState(title, description, actionNode) {
    return el('div', { className: 'app-empty' }, [
      el('p', { className: 'app-empty__title', text: title }),
      description ? el('p', { className: 'app-empty__desc', text: description }) : null,
      actionNode ? el('div', { className: 'app-empty__action' }, actionNode) : null
    ]);
  }

  /** 오류 상태 블록을 만든다. */
  function errorState(title, description, retryHandler) {
    return el('div', { className: 'app-error-state' }, [
      el('p', { className: 'app-error-state__title', text: title }),
      description ? el('p', { className: 'app-error-state__desc', text: description }) : null,
      retryHandler
        ? el('div', { className: 'app-empty__action' }, [
            el('button', {
              className: 'app-btn app-btn--secondary app-btn--sm',
              text: '다시 시도',
              attrs: { type: 'button' },
              on: { click: retryHandler }
            })
          ])
        : null
    ]);
  }

  /** 로딩 중 표시. rows 를 주면 스켈레톤, 없으면 스피너 한 줄. */
  function loadingBlock(rows) {
    if (!rows) {
      return el('div', { className: 'app-loading-block' }, [
        el('span', { className: 'app-spinner' }),
        el('span', { text: '불러오는 중입니다.' })
      ]);
    }
    var wrap = el('div', { className: 'app-skeleton-list' });
    for (var i = 0; i < rows; i += 1) {
      wrap.appendChild(el('div', { className: 'app-skeleton' }));
    }
    return wrap;
  }

  /** 테이블 본문을 "데이터 없음" 한 줄로 채운다. */
  function tableMessageRow(colSpan, message) {
    return el('tr', {}, [
      el('td', {
        className: 'app-muted',
        attrs: { colspan: colSpan, style: 'text-align:center;padding:34px 14px' },
        text: message
      })
    ]);
  }

  /** 버튼을 로딩 상태로 바꾼다. 중복 클릭 방지를 겸한다. */
  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      if (button.dataset.loading === 'true') return;
      button.dataset.loading = 'true';
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      var spinner = el('span', { className: 'app-spinner' });
      spinner.dataset.role = 'busy-spinner';
      button.insertBefore(spinner, button.firstChild);
    } else {
      button.dataset.loading = 'false';
      button.disabled = false;
      button.removeAttribute('aria-busy');
      var existing = button.querySelector('[data-role="busy-spinner"]');
      if (existing) existing.remove();
    }
  }

  /**
   * 비동기 처리 중 버튼을 잠근다. 중복 클릭으로 삭제 작업이 두 번 생성되는 사고를 막는다.
   * 반환값은 handler 의 결과이며 예외는 그대로 다시 던진다.
   */
  function guard(button, handler) {
    if (button && button.dataset.loading === 'true') return Promise.resolve(undefined);
    setBusy(button, true);
    return Promise.resolve()
      .then(handler)
      .then(
        function (result) {
          setBusy(button, false);
          return result;
        },
        function (error) {
          setBusy(button, false);
          throw error;
        }
      );
  }

  function debounce(fn, waitMs) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fn.apply(self, args);
      }, waitMs || 200);
    };
  }

  // ==========================================================================
  // 토스트
  // ==========================================================================

  var TOAST_TIMEOUT = { success: 3200, info: 3800, error: 6000 };

  /** 화면 우하단에 알림을 띄운다. type: success | error | info */
  function toast(message, type) {
    var area = document.getElementById('app-toast-area');
    if (!area) return;
    var tone = type || 'info';

    var closeBtn = el('button', {
      className: 'app-toast__close',
      attrs: { type: 'button', 'aria-label': '알림 닫기' }
    }, [closeIcon()]);

    var node = el('div', { className: 'app-toast app-toast--' + tone }, [
      el('span', { className: 'app-toast__bar' }),
      el('span', { className: 'app-toast__msg', text: message }),
      closeBtn
    ]);

    var timer = null;
    function dismiss() {
      if (timer) clearTimeout(timer);
      if (node.parentNode) node.parentNode.removeChild(node);
    }
    closeBtn.addEventListener('click', dismiss);
    area.appendChild(node);
    timer = setTimeout(dismiss, TOAST_TIMEOUT[tone] || 4000);
  }

  // ==========================================================================
  // API 래퍼
  // ==========================================================================

  /**
   * JSON fetch 래퍼.
   * 오류 응답의 error.message 를 토스트로 띄우고 ApiError 를 던진다.
   * 호출부는 성공 경로만 작성하고, 실패는 catch 에서 화면 상태만 정리하면 된다.
   */
  function request(method, url, body, options) {
    var opts = options || {};
    var init = {
      method: method,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (opts.signal) init.signal = opts.signal;

    return fetch(url, init).then(
      function (response) {
        return response.text().then(function (text) {
          var data = null;
          if (text) {
            try {
              data = JSON.parse(text);
            } catch (parseError) {
              data = null;
            }
          }

          if (!response.ok) {
            // 인증이 켜진 배포에서 세션이 끊기면 로그인 화면이 HTML 로 돌아온다.
            if (response.status === 401 && data === null) {
              global.location.href = '/login';
            }
            var message =
              (data && data.error && data.error.message) ||
              (data && data.message) ||
              ('요청이 실패했습니다. (HTTP ' + response.status + ')');
            if (!opts.silent) toast(message, 'error');
            throw ApiError(message, response.status, data);
          }
          return data;
        });
      },
      function (networkError) {
        if (networkError && networkError.name === 'AbortError') throw networkError;
        var message = '서버에 연결하지 못했습니다. 프로그램이 실행 중인지 확인하세요.';
        if (!opts.silent) toast(message, 'error');
        throw ApiError(message, 0, null);
      }
    );
  }

  var api = {
    get: function (url, options) {
      return request('GET', url, undefined, options);
    },
    post: function (url, body, options) {
      return request('POST', url, body === undefined ? {} : body, options);
    },
    patch: function (url, body, options) {
      return request('PATCH', url, body === undefined ? {} : body, options);
    },
    put: function (url, body, options) {
      return request('PUT', url, body === undefined ? {} : body, options);
    },
    del: function (url, body, options) {
      return request('DELETE', url, body, options);
    }
  };

  /** 객체를 질의 문자열로 만든다. 빈 값과 빈 배열은 제외한다. */
  function buildQuery(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') return;
      if (Array.isArray(value)) {
        if (!value.length) return;
        parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value.join(',')));
        return;
      }
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    return parts.join('&');
  }

  // ==========================================================================
  // 모달
  // ==========================================================================

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  /**
   * 확인 모달을 띄우고 Promise<boolean> 을 돌려준다.
   *
   * options
   *   title        : 제목
   *   body         : 문자열(줄바꿈으로 문단 분리) 또는 DOM 노드
   *   confirmText  : 실행 버튼 문구 (기본 '확인')
   *   cancelText   : 취소 버튼 문구 (기본 '취소')
   *   danger       : true 면 실행 버튼을 빨강으로
   *   requireTyping: 문자열. 사용자가 정확히 이 값을 입력해야 실행 버튼이 열린다
   *   requireCheckbox: 문자열. 이 라벨의 체크박스를 켜야 실행 버튼이 열린다.
   *                    requireTyping 과 함께 쓰면 두 조건을 모두 만족해야 한다
   */
  function confirmModal(options) {
    var config = options || {};
    var root = document.getElementById('app-modal-root');
    if (!root) return Promise.resolve(global.confirm(config.title || '계속할까요?'));

    return new Promise(function (resolve) {
      var previousFocus = document.activeElement;
      var settled = false;

      var confirmBtn = el('button', {
        className: 'app-btn ' + (config.danger ? 'app-btn--danger' : 'app-btn--primary'),
        text: config.confirmText || '확인',
        attrs: { type: 'button' }
      });
      var cancelBtn = el('button', {
        className: 'app-btn app-btn--secondary',
        text: config.cancelText || '취소',
        attrs: { type: 'button' }
      });

      var bodyNode = el('div', { className: 'app-modal__body' });
      if (config.body instanceof Node) {
        bodyNode.appendChild(config.body);
      } else if (config.body) {
        String(config.body)
          .split('\n')
          .forEach(function (line) {
            bodyNode.appendChild(el('p', { text: line }));
          });
      }

      // 실행 버튼을 여는 조건이 여러 개일 수 있으므로 한곳에서 모아 판정한다.
      var gates = [];
      function refreshGate() {
        confirmBtn.disabled = gates.some(function (isOpen) {
          return !isOpen();
        });
      }

      var checkboxInput = null;
      if (config.requireCheckbox) {
        var checkId = 'app-confirm-checkbox';
        checkboxInput = el('input', {
          className: 'app-check',
          attrs: { type: 'checkbox', id: checkId }
        });
        bodyNode.appendChild(
          el('div', { className: 'app-modal__typing' }, [
            el('label', { className: 'app-check-label', attrs: { for: checkId } }, [
              checkboxInput,
              el('span', { text: config.requireCheckbox })
            ])
          ])
        );
        checkboxInput.addEventListener('change', refreshGate);
        gates.push(function () {
          return checkboxInput.checked;
        });
      }

      var typingInput = null;
      if (config.requireTyping) {
        var inputId = 'app-confirm-typing';
        typingInput = el('input', {
          className: 'app-input',
          attrs: {
            type: 'text',
            id: inputId,
            autocomplete: 'off',
            placeholder: config.requireTyping,
            'aria-describedby': inputId + '-hint'
          }
        });
        bodyNode.appendChild(
          el('div', { className: 'app-modal__typing' }, [
            el('label', {
              className: 'app-field__label',
              text: '확인을 위해 아래 입력창에 ' + config.requireTyping + ' 를 그대로 입력하세요.',
              attrs: { for: inputId }
            }),
            typingInput,
            el('span', {
              className: 'app-field__hint',
              text: '입력이 정확히 일치해야 실행 버튼이 활성화됩니다.',
              attrs: { id: inputId + '-hint' }
            })
          ])
        );
        typingInput.addEventListener('input', refreshGate);
        typingInput.addEventListener('keydown', function (event) {
          if (event.key === 'Enter' && !confirmBtn.disabled) {
            event.preventDefault();
            finish(true);
          }
        });
        gates.push(function () {
          return typingInput.value.trim() === config.requireTyping;
        });
      }

      // 조건이 하나라도 있으면 처음에는 잠가 둔다.
      refreshGate();

      var dialog = el(
        'div',
        {
          className: 'app-modal' + (config.wide ? ' app-modal--wide' : ''),
          attrs: {
            role: 'dialog',
            'aria-modal': 'true',
            'aria-labelledby': 'app-modal-title'
          }
        },
        [
          el('div', { className: 'app-modal__head' }, [
            el('h2', {
              className: 'app-modal__title',
              text: config.title || '확인',
              attrs: { id: 'app-modal-title' }
            })
          ]),
          bodyNode,
          el('div', { className: 'app-modal__foot' }, [cancelBtn, confirmBtn])
        ]
      );

      var backdrop = el('div', { className: 'app-modal-backdrop' }, [dialog]);

      function onKeydown(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          finish(false);
          return;
        }
        if (event.key !== 'Tab') return;
        // 포커스 트랩: 모달 밖으로 탭이 빠져나가지 않게 한다.
        var focusables = qsa(FOCUSABLE, dialog).filter(function (node) {
          return node.offsetParent !== null || node === document.activeElement;
        });
        if (!focusables.length) return;
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }

      function finish(result) {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKeydown, true);
        clear(root);
        root.hidden = true;
        if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
        resolve(result);
      }

      confirmBtn.addEventListener('click', function () {
        if (confirmBtn.disabled) return;
        finish(true);
      });
      cancelBtn.addEventListener('click', function () {
        finish(false);
      });
      backdrop.addEventListener('mousedown', function (event) {
        if (event.target === backdrop) finish(false);
      });
      document.addEventListener('keydown', onKeydown, true);

      clear(root);
      root.hidden = false;
      root.appendChild(backdrop);
      (checkboxInput || typingInput || confirmBtn).focus();
    });
  }

  // ==========================================================================
  // SSE (작업 진행률)
  // ==========================================================================

  /**
   * 단일 작업의 진행률 스트림을 구독한다. GET /api/jobs/{id}/stream
   *
   * handlers: { onProgress(payload), onDone(payload), onError(info) }
   * 반환: { close() }
   *
   * EventSource 는 끊기면 자동으로 재연결하지만, 서버가 종료된 경우 무한 재시도가 되므로
   * 연속 실패 횟수를 세어 한계를 넘으면 스스로 닫고 onError 를 호출한다.
   */
  function subscribeJobProgress(jobId, handlers) {
    return openStream('/api/jobs/' + encodeURIComponent(jobId) + '/stream', handlers);
  }

  /** 실행 중인 전체 작업 스트림을 구독한다. GET /api/jobs/stream */
  function subscribeJobsStream(handlers) {
    return openStream('/api/jobs/stream', handlers);
  }

  function openStream(url, handlers) {
    var callbacks = handlers || {};
    var MAX_FAILURES = 5;
    var failures = 0;
    var closed = false;
    var source = null;

    function parse(event) {
      try {
        return JSON.parse(event.data);
      } catch (parseError) {
        return null;
      }
    }

    function connect() {
      if (closed) return;
      source = new EventSource(url);

      source.addEventListener('open', function () {
        failures = 0;
      });

      source.addEventListener('progress', function (event) {
        failures = 0;
        var payload = parse(event);
        if (payload && callbacks.onProgress) callbacks.onProgress(payload);
      });

      source.addEventListener('done', function (event) {
        var payload = parse(event);
        controller.close();
        if (callbacks.onDone) callbacks.onDone(payload);
      });

      // 서버가 명시적으로 보내는 오류 이벤트. 브라우저 연결 오류와 구분해 처리한다.
      source.addEventListener('failed', function (event) {
        var payload = parse(event);
        controller.close();
        if (callbacks.onError) callbacks.onError({ reason: 'server', payload: payload });
      });

      source.onerror = function () {
        if (closed) return;
        // readyState 가 CLOSED 면 브라우저가 재연결을 포기한 상태다.
        failures += 1;
        if (source && source.readyState === EventSource.CLOSED) {
          if (failures >= MAX_FAILURES) {
            controller.close();
            if (callbacks.onError) {
              callbacks.onError({ reason: 'disconnected', payload: null });
            }
            return;
          }
          // 지수 백오프로 직접 재연결한다.
          var delay = Math.min(1000 * Math.pow(2, failures - 1), 8000);
          setTimeout(function () {
            if (!closed) connect();
          }, delay);
        }
      };
    }

    var controller = {
      close: function () {
        closed = true;
        if (source) {
          source.onerror = null;
          source.close();
          source = null;
        }
      }
    };

    connect();
    return controller;
  }

  // ==========================================================================
  // 작업 진행률 패널 (게시글 수집, 댓글 삭제 화면이 공유)
  // ==========================================================================

  var JOB_STATUS_LABEL = {
    pending: '대기 중',
    running: '진행 중',
    paused: '일시정지',
    completed: '완료',
    failed: '실패',
    cancelled: '취소됨'
  };

  var JOB_STATUS_TONE = {
    pending: 'gray',
    running: 'blue',
    paused: 'amber',
    completed: 'green',
    failed: 'red',
    cancelled: 'gray'
  };

  var JOB_TYPE_LABEL = {
    collect: '댓글 수집',
    delete: '댓글 삭제',
    discover: '게시글 탐색'
  };

  /**
   * 진행률 패널을 컨테이너에 붙이고 작업 하나를 추적한다.
   *
   * 반환 객체
   *   track(jobId, { label, onDone, onProgress }) - 구독 시작
   *   update(payload)                            - 외부에서 받은 진행 상황 반영
   *   hide()                                     - 패널 감추기
   *   stop()                                     - 구독 해제
   */
  function jobProgressPanel(container) {
    var titleNode = el('span', { className: 'app-section-title', text: '작업 진행 상황' });
    var statusNode = badge('대기 중', 'gray');
    var bar = el('div', { className: 'app-progress__bar' });
    var metaNode = el('div', { className: 'app-progress__meta' });
    var resultNode = el('div', { className: 'app-progress__meta' });

    var panel = el('div', { className: 'app-job-panel app-hidden' }, [
      el('div', { className: 'app-row', attrs: { style: 'margin-bottom:10px' } }, [
        titleNode,
        statusNode
      ]),
      el('div', { className: 'app-progress' }, [
        el('div', { className: 'app-progress__track' }, [bar]),
        metaNode,
        resultNode
      ])
    ]);
    container.appendChild(panel);

    var stream = null;
    var currentJobId = null;

    function applyStatus(status) {
      var label = JOB_STATUS_LABEL[status] || status || '진행 중';
      var tone = JOB_STATUS_TONE[status] || 'blue';
      statusNode.className = 'app-badge app-badge--' + tone;
      statusNode.textContent = label;
      bar.className =
        'app-progress__bar' +
        (status === 'failed' ? ' app-progress__bar--danger'
          : status === 'completed' ? ' app-progress__bar--success'
          : status === 'paused' ? ' app-progress__bar--warn'
          : '');
    }

    function update(payload) {
      if (!payload) return;
      var total = Number(payload.total || 0);
      var done = Number(payload.done || 0);
      var percent = payload.percent !== undefined && payload.percent !== null
        ? Number(payload.percent)
        : (total > 0 ? (done / total) * 100 : 0);

      panel.classList.remove('app-hidden');
      bar.style.width = Math.max(0, Math.min(100, percent)) + '%';
      applyStatus(payload.status);

      clear(metaNode);
      metaNode.appendChild(
        el('span', {
          text: formatNumber(done) + ' / ' + formatNumber(total) + '건  ' + formatPercent(percent)
        })
      );
      if (payload.message) {
        metaNode.appendChild(el('span', { className: 'app-muted-soft', text: payload.message }));
      }

      clear(resultNode);
      if (payload.succeeded !== undefined) {
        resultNode.appendChild(
          el('span', { text: '성공 ' + formatNumber(payload.succeeded || 0) + '건' })
        );
        resultNode.appendChild(
          el('span', { text: '실패 ' + formatNumber(payload.failed || 0) + '건' })
        );
        resultNode.appendChild(
          el('span', { text: '건너뜀 ' + formatNumber(payload.skipped || 0) + '건' })
        );
      }
    }

    function stop() {
      if (stream) {
        stream.close();
        stream = null;
      }
    }

    function track(jobId, options) {
      var config = options || {};
      stop();
      currentJobId = jobId;
      panel.classList.remove('app-hidden');
      titleNode.textContent = config.label || '작업 진행 상황';
      clear(resultNode);
      bar.style.width = '0%';
      applyStatus('running');
      clear(metaNode);
      metaNode.appendChild(el('span', { text: '작업 ' + jobId + '번을 시작했습니다.' }));

      stream = subscribeJobProgress(jobId, {
        onProgress: function (payload) {
          update(payload);
          if (config.onProgress) config.onProgress(payload);
        },
        onDone: function (payload) {
          if (payload) update(payload);
          else applyStatus('completed');
          stream = null;
          if (config.onDone) config.onDone(payload);
        },
        onError: function (info) {
          stream = null;
          clear(resultNode);
          resultNode.appendChild(
            el('span', {
              className: 'app-muted-soft',
              text:
                info && info.reason === 'server'
                  ? '작업 스트림에서 오류가 전달되었습니다. 작업 이력에서 상세를 확인하세요.'
                  : '진행률 연결이 끊겼습니다. 작업 이력 화면에서 상태를 확인하세요.'
            })
          );
          if (config.onError) config.onError(info);
        }
      });
    }

    return {
      node: panel,
      track: track,
      update: update,
      stop: stop,
      jobId: function () {
        return currentJobId;
      },
      hide: function () {
        stop();
        panel.classList.add('app-hidden');
      }
    };
  }

  // ==========================================================================
  // 공통 라벨
  // ==========================================================================

  var SPAM_LEVEL = {
    spam: { label: '스팸', tone: 'red' },
    suspicious: { label: '의심', tone: 'amber' },
    normal: { label: '정상', tone: 'gray' }
  };

  var COMMENT_STATUS = {
    active: { label: '활성', tone: 'blue' },
    deleting: { label: '삭제 중', tone: 'amber' },
    deleted: { label: '삭제됨', tone: 'green' },
    failed: { label: '삭제 실패', tone: 'red' }
  };

  var AUTH_STATE = {
    owner: { label: '소유자 인증됨', tone: 'green' },
    not_owner: { label: '소유자 아님', tone: 'amber' },
    anonymous: { label: '로그인 필요', tone: 'amber' },
    missing: { label: '쿠키 미등록', tone: 'red' },
    unknown: { label: '세션 진단 필요', tone: 'gray' }
  };

  // ==========================================================================
  // 노출
  // ==========================================================================

  global.TG = {
    // 데이터
    api: api,
    buildQuery: buildQuery,
    ApiError: ApiError,
    // 화면 피드백
    toast: toast,
    confirmModal: confirmModal,
    // 포맷터
    escapeHtml: escapeHtml,
    formatNumber: formatNumber,
    formatDateTime: formatDateTime,
    formatDate: formatDate,
    formatBytes: formatBytes,
    formatPercent: formatPercent,
    baseName: baseName,
    truncate: truncate,
    // DOM
    el: el,
    qs: qs,
    qsa: qsa,
    svgIcon: svgIcon,
    clear: clear,
    badge: badge,
    emptyState: emptyState,
    errorState: errorState,
    loadingBlock: loadingBlock,
    tableMessageRow: tableMessageRow,
    setBusy: setBusy,
    guard: guard,
    debounce: debounce,
    // 작업
    subscribeJobProgress: subscribeJobProgress,
    subscribeJobsStream: subscribeJobsStream,
    jobProgressPanel: jobProgressPanel,
    // 라벨
    JOB_STATUS_LABEL: JOB_STATUS_LABEL,
    JOB_STATUS_TONE: JOB_STATUS_TONE,
    JOB_TYPE_LABEL: JOB_TYPE_LABEL,
    SPAM_LEVEL: SPAM_LEVEL,
    COMMENT_STATUS: COMMENT_STATUS,
    AUTH_STATE: AUTH_STATE,
    // 설정
    pageSize: function () {
      var value = parseInt(document.body.getAttribute('data-page-size'), 10);
      return isFinite(value) && value > 0 ? value : 50;
    },
    blogUrl: function () {
      return document.body.getAttribute('data-blog-url') || '';
    }
  };
})(window);
