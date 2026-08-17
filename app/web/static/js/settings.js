// app/web/static/js/settings.js
//
// 설정 화면.
//
// 호출 API
//   GET    /api/settings
//   POST   /api/settings/cookies          { raw }
//   POST   /api/settings/cookies/browser
//   DELETE /api/settings/cookies
//   POST   /api/settings/diagnose
//   POST   /api/settings/test-delete      { comment_id }
//   GET    /api/settings/rules
//   PUT    /api/settings/rules            { yaml }
//   GET    /api/backups
//
// 쿠키 값은 어떤 경로로도 화면에 출력하지 않는다. 서버 응답의 cookie_names 만 표시한다.

(function () {
  'use strict';

  var el = TG.el;
  var clear = TG.clear;
  var BACKUP_COLSPAN = 4;

  var refs = {
    refresh: document.getElementById('app-settings-refresh'),

    authBadge: document.getElementById('app-auth-badge'),
    authMessage: document.getElementById('app-auth-message'),
    cookieNames: document.getElementById('app-cookie-names'),
    cookieRaw: document.getElementById('app-cookie-raw'),
    cookieSave: document.getElementById('app-cookie-save'),
    cookieBrowser: document.getElementById('app-cookie-browser'),
    cookieDiagnose: document.getElementById('app-cookie-diagnose'),
    cookieDelete: document.getElementById('app-cookie-delete'),
    cookieResult: document.getElementById('app-cookie-result'),

    testId: document.getElementById('app-test-delete-id'),
    testBtn: document.getElementById('app-test-delete-btn'),
    testResult: document.getElementById('app-test-delete-result'),

    runtimeKv: document.getElementById('app-runtime-kv'),
    paths: document.getElementById('app-paths'),

    rulesYaml: document.getElementById('app-rules-yaml'),
    rulesSave: document.getElementById('app-rules-save'),
    rulesReload: document.getElementById('app-rules-reload'),
    rulesResult: document.getElementById('app-rules-result'),

    backupsBody: document.getElementById('app-backups-body'),
    backupsSummary: document.getElementById('app-backups-summary'),
    backupsRefresh: document.getElementById('app-backups-refresh')
  };

  var savedRulesYaml = '';

  // ==========================================================================
  // 세션 상태
  // ==========================================================================

  function renderAuth(auth) {
    var info = TG.AUTH_STATE[auth && auth.state] || TG.AUTH_STATE.unknown;
    refs.authBadge.className = 'app-badge app-badge--' + info.tone + ' app-badge--lg';
    refs.authBadge.textContent = info.label;

    refs.authMessage.textContent =
      (auth && auth.message) || '세션을 아직 진단하지 않았습니다. 세션 진단을 실행하세요.';

    var names = (auth && auth.cookie_names) || [];
    clear(refs.cookieNames);
    if (!names.length) {
      refs.cookieNames.className = 'app-muted-soft app-text-sm';
      refs.cookieNames.textContent = '없음';
    } else {
      refs.cookieNames.className = 'app-row';
      names.forEach(function (name) {
        refs.cookieNames.appendChild(TG.badge(name, 'gray'));
      });
      if (auth && auth.checked_at) {
        refs.cookieNames.appendChild(
          el('span', {
            className: 'app-muted-soft app-text-sm',
            text: '마지막 진단 ' + TG.formatDateTime(auth.checked_at)
          })
        );
      }
    }

    // 상단 헤더의 배지도 함께 맞춘다. 새로 고침 없이 상태가 일치하도록.
    var topBadge = document.querySelector('.app-topbar__right .app-badge');
    if (topBadge) {
      topBadge.className = 'app-badge app-badge--' + info.tone + ' app-badge--lg';
      clear(topBadge);
      topBadge.appendChild(el('span', { className: 'app-badge__dot' }));
      topBadge.appendChild(document.createTextNode(info.label));
      topBadge.title = (auth && auth.message) || '';
    }
  }

  // ==========================================================================
  // 실행 설정과 경로
  // ==========================================================================

  var RUNTIME_LABELS = [
    ['collect_concurrency', '수집 동시 실행', '개'],
    ['collect_rps', '수집 초당 요청', 'RPS'],
    ['delete_concurrency', '삭제 동시 실행', '개'],
    ['delete_rps', '삭제 초당 요청', 'RPS'],
    ['delete_dry_run', '삭제 드라이런 기본값', ''],
    ['circuit_breaker_threshold', '서킷 브레이커 임계', '회'],
    ['backup_before_delete', '삭제 전 백업', ''],
    ['page_size', '목록 페이지 크기', '건'],
    ['timezone', '시간대', ''],
    ['monitor_enabled', '주기 모니터링', ''],
    ['monitor_interval_minutes', '모니터링 주기', '분']
  ];

  var PATH_LABELS = [
    ['env_file', '.env 파일'],
    ['rules_file', '규칙 파일'],
    ['targets_file', '대상 목록 파일'],
    ['backup_dir', '백업 폴더'],
    ['database', '데이터베이스'],
    ['log_dir', '로그 폴더']
  ];

  function formatRuntimeValue(value, unit) {
    if (typeof value === 'boolean') return value ? '켜짐' : '꺼짐';
    if (value === null || value === undefined) return '-';
    var text = typeof value === 'number' ? TG.formatNumber(value) : String(value);
    return unit ? text + ' ' + unit : text;
  }

  function renderRuntime(runtime) {
    clear(refs.runtimeKv);
    if (!runtime) {
      refs.runtimeKv.appendChild(
        el('div', { className: 'app-kv__item' }, [
          el('span', { className: 'app-kv__key', text: '실행 설정' }),
          el('span', { className: 'app-kv__val', text: '불러오지 못했습니다.' })
        ])
      );
      return;
    }
    RUNTIME_LABELS.forEach(function (entry) {
      var key = entry[0];
      if (!(key in runtime)) return;
      refs.runtimeKv.appendChild(
        el('div', { className: 'app-kv__item' }, [
          el('span', { className: 'app-kv__key', text: entry[1] }),
          el('span', { className: 'app-kv__val', text: formatRuntimeValue(runtime[key], entry[2]) })
        ])
      );
    });
  }

  function renderPaths(paths) {
    clear(refs.paths);
    if (!paths) return;
    PATH_LABELS.forEach(function (entry) {
      var value = paths[entry[0]];
      if (!value) return;
      refs.paths.appendChild(
        el('div', { className: 'app-row' }, [
          el('span', { className: 'app-field__label', attrs: { style: 'min-width:110px' }, text: entry[1] }),
          el('span', { className: 'app-path', text: String(value) })
        ])
      );
    });
  }

  // ==========================================================================
  // 데이터 로드
  // ==========================================================================

  function loadSettings() {
    return TG.api
      .get('/api/settings')
      .then(function (data) {
        renderAuth(data && data.auth);
        renderRuntime(data && data.runtime);
        renderPaths(data && data.paths);
      })
      .catch(function () {
        refs.authMessage.textContent = '설정을 불러오지 못했습니다.';
      });
  }

  function loadRules() {
    return TG.api
      .get('/api/settings/rules')
      .then(function (data) {
        savedRulesYaml = (data && data.yaml) || '';
        refs.rulesYaml.value = savedRulesYaml;
        clear(refs.rulesResult);
      })
      .catch(function (error) {
        clear(refs.rulesResult);
        refs.rulesResult.appendChild(TG.errorState('규칙을 불러오지 못했습니다.', error.message));
      });
  }

  function loadBackups() {
    clear(refs.backupsBody);
    refs.backupsBody.appendChild(TG.tableMessageRow(BACKUP_COLSPAN, '백업 목록을 불러오는 중입니다.'));

    return TG.api
      .get('/api/backups', { silent: true })
      .then(function (data) {
        var items = (data && data.items) || [];
        clear(refs.backupsBody);

        if (!items.length) {
          refs.backupsBody.appendChild(
            TG.tableMessageRow(BACKUP_COLSPAN, '생성된 백업 파일이 없습니다.')
          );
          refs.backupsSummary.textContent = '삭제 전에 생성된 백업 파일 목록입니다.';
          return;
        }

        items.forEach(function (file) {
          var name = file.name || '';
          refs.backupsBody.appendChild(
            el('tr', {}, [
              el('td', {}, [
                el('span', { className: 'app-truncate app-cell-content', text: name, title: name })
              ]),
              el('td', { className: 'app-num', text: TG.formatBytes(file.size) }),
              el('td', { className: 'app-nowrap', text: TG.formatDateTime(file.created_at) }),
              el('td', { className: 'app-nowrap' }, [
                el('a', {
                  className: 'app-btn app-btn--secondary app-btn--sm',
                  text: '내려받기',
                  attrs: {
                    href: '/api/backups/' + encodeURIComponent(name),
                    download: name
                  }
                })
              ])
            ])
          );
        });

        refs.backupsSummary.textContent = '총 ' + TG.formatNumber(items.length) + '개 파일';
      })
      .catch(function () {
        clear(refs.backupsBody);
        refs.backupsBody.appendChild(
          TG.tableMessageRow(BACKUP_COLSPAN, '백업 목록을 불러오지 못했습니다.')
        );
      });
  }

  // ==========================================================================
  // 쿠키 동작
  // ==========================================================================

  /** 진단 응답을 상태 배지와 메시지에 반영한다. auth 를 감싸든 그대로든 받아들인다. */
  function applyDiagnose(data, successMessage) {
    var auth = (data && data.auth) || data;
    renderAuth(auth);
    clear(refs.cookieResult);

    var info = TG.AUTH_STATE[auth && auth.state] || TG.AUTH_STATE.unknown;
    var isOwner = auth && auth.state === 'owner';
    refs.cookieResult.appendChild(
      el('div', { className: isOwner ? 'app-banner' : 'app-banner app-banner--warn' }, [
        TG.badge(info.label, info.tone),
        el('span', { text: (auth && auth.message) || successMessage || '진단을 마쳤습니다.' })
      ])
    );
    TG.toast(successMessage || '진단을 마쳤습니다.', isOwner ? 'success' : 'info');
  }

  refs.cookieSave.addEventListener('click', function () {
    var raw = refs.cookieRaw.value.trim();
    if (!raw) {
      TG.toast('쿠키 내용을 붙여넣으세요.', 'error');
      refs.cookieRaw.focus();
      return;
    }
    TG.guard(refs.cookieSave, function () {
      return TG.api.post('/api/settings/cookies', { raw: raw }).then(function (data) {
        // 입력창에 세션 값이 남아 있지 않도록 즉시 비운다.
        refs.cookieRaw.value = '';
        applyDiagnose(data, '쿠키를 등록했습니다.');
      });
    }).catch(function () {});
  });

  refs.cookieBrowser.addEventListener('click', function () {
    TG.guard(refs.cookieBrowser, function () {
      return TG.api.post('/api/settings/cookies/browser', {}).then(function (data) {
        applyDiagnose(data, '브라우저에서 쿠키를 가져왔습니다.');
      });
    }).catch(function () {});
  });

  refs.cookieDiagnose.addEventListener('click', function () {
    TG.guard(refs.cookieDiagnose, function () {
      return TG.api.post('/api/settings/diagnose', {}).then(function (data) {
        applyDiagnose(data, '세션 진단을 마쳤습니다.');
      });
    }).catch(function () {});
  });

  refs.cookieDelete.addEventListener('click', function () {
    TG.confirmModal({
      title: '등록된 쿠키를 삭제할까요?',
      body:
        '저장된 세션 쿠키를 지웁니다.\n삭제 기능을 다시 쓰려면 쿠키를 새로 등록해야 합니다.\n' +
        '댓글 수집은 쿠키 없이도 계속 동작합니다.',
      confirmText: '쿠키 삭제',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.cookieDelete, function () {
        return TG.api.del('/api/settings/cookies').then(function () {
          clear(refs.cookieResult);
          TG.toast('쿠키를 삭제했습니다.', 'success');
          return loadSettings();
        });
      }).catch(function () {});
    });
  });

  // ==========================================================================
  // 1건 시험 삭제
  // ==========================================================================

  function renderTestResult(data, isError, message) {
    clear(refs.testResult);
    var text;
    try {
      text = JSON.stringify(data, null, 2);
    } catch (serializeError) {
      text = String(data);
    }

    var nodes = [];
    // 보호 대상 거부처럼 서버가 이유를 설명한 경우, 원문 JSON 에 묻히지 않게 먼저 보여준다.
    if (isError && message) {
      nodes.push(TG.errorState('시험 삭제를 실행하지 않았습니다.', message));
    }
    nodes.push(
      el('span', {
        className: 'app-field__label',
        text: isError ? '실패 응답 원문' : '응답 원문'
      })
    );
    // pre 에도 textContent 로만 넣는다. 응답 본문에 HTML 이 섞여 있을 수 있다.
    nodes.push(el('pre', { className: 'app-pre', text: text }));

    refs.testResult.appendChild(el('div', { className: 'app-stack app-stack--sm' }, nodes));
  }

  refs.testBtn.addEventListener('click', function () {
    var value = parseInt(refs.testId.value, 10);
    if (!isFinite(value) || value <= 0) {
      TG.toast('삭제를 시험할 댓글 번호를 입력하세요.', 'error');
      refs.testId.focus();
      return;
    }

    TG.confirmModal({
      title: '댓글 1건을 실제로 삭제합니다.',
      body:
        '댓글 번호 ' + value + ' 을(를) 블로그에서 삭제합니다.\n' +
        '드라이런이 아니며 되돌릴 수 없습니다.\n' +
        '이 확인은 대량 삭제 전에 삭제 응답 형태를 검증하기 위한 절차입니다.',
      confirmText: '시험 삭제 실행',
      danger: true,
      requireTyping: '삭제'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.testBtn, function () {
        return TG.api
          .post('/api/settings/test-delete', { comment_id: value }, { silent: true })
          .then(function (data) {
            renderTestResult(data, false);
            TG.toast('시험 삭제를 실행했습니다. 응답 내용을 확인하세요.', 'success');
          })
          .catch(function (error) {
            renderTestResult(error.payload || { message: error.message }, true, error.message);
            TG.toast(error.message, 'error');
            throw error;
          });
      }).catch(function () {});
    });
  });

  // ==========================================================================
  // 규칙 편집
  // ==========================================================================

  refs.rulesSave.addEventListener('click', function () {
    var yaml = refs.rulesYaml.value;
    if (!yaml.trim()) {
      TG.toast('규칙 내용이 비어 있습니다.', 'error');
      refs.rulesYaml.focus();
      return;
    }

    TG.guard(refs.rulesSave, function () {
      clear(refs.rulesResult);
      // silent: 검증 실패 메시지는 화면에 원문 그대로 남겨야 하므로 토스트에만 맡기지 않는다.
      return TG.api
        .put('/api/settings/rules', { yaml: yaml }, { silent: true })
        .then(function () {
          savedRulesYaml = yaml;
          TG.toast('규칙을 저장했습니다.', 'success');
          clear(refs.rulesResult);
          refs.rulesResult.appendChild(
            el('div', { className: 'app-banner' }, [
              el('span', {
                text:
                  '저장했습니다. 이미 수집된 댓글에 새 규칙을 적용하려면 댓글 관리 화면에서 스팸 재분류를 실행하세요.'
              })
            ])
          );
        })
        .catch(function (error) {
          // 서버가 준 검증 오류를 가공하지 않고 그대로 보여준다.
          clear(refs.rulesResult);
          var detail = error.payload && error.payload.error && error.payload.error.message
            ? error.payload.error.message
            : error.message;
          refs.rulesResult.appendChild(TG.errorState('규칙을 저장하지 못했습니다.', detail));
          if (error.payload && error.payload.error && error.payload.error.type) {
            refs.rulesResult.appendChild(
              el('pre', {
                className: 'app-pre',
                attrs: { style: 'margin-top:10px' },
                text: error.payload.error.type + ': ' + detail
              })
            );
          }
          TG.toast('규칙 저장에 실패했습니다. 아래 오류 내용을 확인하세요.', 'error');
          throw error;
        });
    }).catch(function () {});
  });

  refs.rulesReload.addEventListener('click', function () {
    // 편집한 내용이 없으면 굳이 확인을 묻지 않는다.
    if (refs.rulesYaml.value === savedRulesYaml) {
      TG.guard(refs.rulesReload, loadRules).catch(function () {});
      return;
    }
    TG.confirmModal({
      title: '편집 내용을 버리고 저장된 규칙으로 되돌릴까요?',
      body: '서버에 저장된 rules.yaml 을 다시 읽어옵니다. 편집 중인 내용은 사라집니다.',
      confirmText: '되돌리기'
    }).then(function (ok) {
      if (!ok) return;
      TG.guard(refs.rulesReload, loadRules).catch(function () {});
    });
  });

  // ==========================================================================
  // 이벤트
  // ==========================================================================

  refs.backupsRefresh.addEventListener('click', function () {
    TG.guard(refs.backupsRefresh, loadBackups);
  });

  refs.refresh.addEventListener('click', function () {
    TG.guard(refs.refresh, function () {
      return Promise.all([loadSettings(), loadBackups()]);
    });
  });

  loadSettings();
  loadRules();
  loadBackups();
})();
