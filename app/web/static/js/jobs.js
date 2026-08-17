// app/web/static/js/jobs.js
//
// 작업 이력 화면.
//
// 호출 API
//   GET  /api/jobs?page=&size=&type=
//   GET  /api/jobs/{id}
//   GET  /api/jobs/{id}/items?status=&page=&size=
//   POST /api/jobs/{id}/cancel
//   POST /api/jobs/{id}/resume
//   POST /api/jobs/{id}/retry-failed
//   GET  /api/jobs/stream            실행 중 작업 실시간 갱신

(function () {
  'use strict';

  var el = TG.el;
  var clear = TG.clear;
  var LIST_COLSPAN = 11;
  var ITEM_COLSPAN = 5;

  var refs = {
    body: document.getElementById('app-jobs-body'),
    summary: document.getElementById('app-jobs-summary'),
    typeChips: document.getElementById('app-job-type-chips'),
    refresh: document.getElementById('app-jobs-refresh'),
    pagerInfo: document.getElementById('app-jobs-pager-info'),
    prev: document.getElementById('app-jobs-prev'),
    next: document.getElementById('app-jobs-next'),

    detail: document.getElementById('app-job-detail'),
    detailTitle: document.getElementById('app-job-detail-title'),
    detailDesc: document.getElementById('app-job-detail-desc'),
    detailActions: document.getElementById('app-job-detail-actions'),
    detailBody: document.getElementById('app-job-detail-body')
  };

  var state = {
    type: '',
    page: 1,
    size: 20,
    total: 0,
    jobs: [],
    selectedJobId: null,
    itemStatus: '',
    itemPage: 1,
    itemSize: 50
  };

  var rowIndex = new Map(); // job_id -> { row, bar, meta, statusCell, counters }

  // ==========================================================================
  // 목록
  // ==========================================================================

  function jobTypeLabel(type) {
    return TG.JOB_TYPE_LABEL[type] || type || '알 수 없음';
  }

  function statusBadge(status) {
    var label = TG.JOB_STATUS_LABEL[status] || status || '-';
    var tone = TG.JOB_STATUS_TONE[status] || 'gray';
    return TG.badge(label, tone);
  }

  function progressCell(job) {
    var bar = el('div', { className: 'app-progress__bar' });
    var meta = el('span', {});
    applyProgress(bar, meta, job);
    return {
      node: el('div', { className: 'app-progress app-progress--inline' }, [
        el('div', { className: 'app-progress__track' }, [bar]),
        el('div', { className: 'app-progress__meta' }, [meta])
      ]),
      bar: bar,
      meta: meta
    };
  }

  function applyProgress(bar, meta, job) {
    var total = Number(job.total || 0);
    var done = Number(job.done || 0);
    var percent =
      job.percent !== undefined && job.percent !== null
        ? Number(job.percent)
        : total > 0 ? (done / total) * 100 : 0;

    bar.style.width = Math.max(0, Math.min(100, percent)) + '%';
    bar.className =
      'app-progress__bar' +
      (job.status === 'failed' ? ' app-progress__bar--danger'
        : job.status === 'completed' ? ' app-progress__bar--success'
        : job.status === 'paused' ? ' app-progress__bar--warn'
        : '');
    meta.textContent =
      TG.formatNumber(done) + ' / ' + TG.formatNumber(total) + '  ' + TG.formatPercent(percent);
  }

  function backupCell(job) {
    if (!job.backup_path) return el('span', { className: 'app-muted-soft', text: '-' });
    var name = TG.baseName(job.backup_path);
    return el('a', {
      text: '내려받기',
      title: job.backup_path,
      attrs: { href: '/api/backups/' + encodeURIComponent(name), download: name }
    });
  }

  function buildRow(job) {
    var progress = progressCell(job);
    var statusCell = el('td', {}, [statusBadge(job.status)]);
    var succeeded = el('td', { className: 'app-num', text: TG.formatNumber(job.succeeded || 0) });
    var failed = el('td', { className: 'app-num' }, [
      job.failed
        ? TG.badge(TG.formatNumber(job.failed), 'red')
        : el('span', { text: '0' })
    ]);
    var skipped = el('td', { className: 'app-num', text: TG.formatNumber(job.skipped || 0) });
    var finished = el('td', { className: 'app-nowrap', text: TG.formatDateTime(job.finished_at) });

    var row = el(
      'tr',
      {
        className: 'app-row--clickable',
        dataset: { jobId: String(job.id) },
        attrs: { tabindex: '0', 'aria-selected': state.selectedJobId === job.id ? 'true' : 'false' }
      },
      [
        el('td', { className: 'app-num app-cell-title', text: String(job.id) }),
        el('td', { className: 'app-nowrap', text: jobTypeLabel(job.type) }),
        statusCell,
        el('td', { attrs: { style: 'min-width:150px' } }, [progress.node]),
        succeeded,
        failed,
        skipped,
        el('td', { className: 'app-nowrap', text: TG.formatDateTime(job.created_at) }),
        el('td', { className: 'app-nowrap', text: TG.formatDateTime(job.started_at) }),
        finished,
        el('td', { className: 'app-nowrap' }, [backupCell(job)])
      ]
    );

    function open() {
      selectJob(job.id);
    }
    row.addEventListener('click', open);
    row.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });

    rowIndex.set(job.id, {
      row: row,
      bar: progress.bar,
      meta: progress.meta,
      statusCell: statusCell,
      succeeded: succeeded,
      failed: failed,
      skipped: skipped,
      finished: finished
    });

    return row;
  }

  function renderList(data) {
    state.jobs = (data && data.items) || [];
    state.total = (data && data.total) || 0;
    state.page = (data && data.page) || state.page;
    state.size = (data && data.size) || state.size;

    rowIndex = new Map();
    clear(refs.body);

    if (!state.jobs.length) {
      refs.body.appendChild(
        TG.tableMessageRow(LIST_COLSPAN, '실행된 작업이 없습니다. 게시글 관리에서 수집을 시작해 보세요.')
      );
      refs.summary.textContent = '작업 기록이 없습니다.';
    } else {
      state.jobs.forEach(function (job) {
        refs.body.appendChild(buildRow(job));
      });
      var running = state.jobs.filter(function (job) {
        return job.status === 'running' || job.status === 'pending';
      }).length;
      refs.summary.textContent =
        '총 ' + TG.formatNumber(state.total) + '건' +
        (running ? ', 진행 중 ' + TG.formatNumber(running) + '건' : '');
    }

    var pages = Math.max(1, Math.ceil(state.total / (state.size || 20)));
    refs.pagerInfo.textContent =
      state.total ? state.page + ' / ' + pages + ' 페이지, 총 ' + TG.formatNumber(state.total) + '건'
        : '표시할 작업이 없습니다.';
    refs.prev.disabled = state.page <= 1;
    refs.next.disabled = state.page >= pages;
  }

  function loadList() {
    clear(refs.body);
    refs.body.appendChild(TG.tableMessageRow(LIST_COLSPAN, '작업 목록을 불러오는 중입니다.'));

    var query = TG.buildQuery({ page: state.page, size: state.size, type: state.type });
    return TG.api
      .get('/api/jobs?' + query)
      .then(renderList)
      .catch(function () {
        clear(refs.body);
        refs.body.appendChild(TG.tableMessageRow(LIST_COLSPAN, '작업 목록을 불러오지 못했습니다.'));
        refs.summary.textContent = '목록을 불러오지 못했습니다.';
      });
  }

  // ==========================================================================
  // 상세
  // ==========================================================================

  function selectJob(jobId) {
    state.selectedJobId = jobId;
    state.itemStatus = '';
    state.itemPage = 1;

    rowIndex.forEach(function (entry, id) {
      entry.row.setAttribute('aria-selected', id === jobId ? 'true' : 'false');
    });

    refs.detail.classList.remove('app-hidden');
    refs.detailTitle.textContent = '작업 ' + jobId + '번 상세';
    refs.detailDesc.textContent = '';
    clear(refs.detailActions);
    clear(refs.detailBody);
    refs.detailBody.appendChild(TG.loadingBlock(4));
    refs.detail.focus();

    loadDetail(jobId);
  }

  function loadDetail(jobId) {
    return TG.api
      .get('/api/jobs/' + encodeURIComponent(jobId))
      .then(function (data) {
        if (state.selectedJobId !== jobId) return;
        // 항목 목록은 renderDetail 이 만드는 섹션이 스스로 불러온다.
        renderDetail(data);
      })
      .catch(function (error) {
        if (state.selectedJobId !== jobId) return;
        clear(refs.detailBody);
        refs.detailBody.appendChild(
          TG.errorState('작업 상세를 불러오지 못했습니다.', error.message, function () {
            selectJob(jobId);
          })
        );
      });
  }

  /** 상세 응답은 {"job": {...}} 형태다. 평면 Job 객체가 와도 동작하도록 둔다. */
  function unwrapJob(data) {
    if (!data) return {};
    if (data.job) return data.job;
    return data;
  }

  function renderDetail(data) {
    var job = unwrapJob(data);
    // GET /api/jobs/{id} 는 {"job": {...}, "counts": {pending, succeeded, failed, skipped}} 형태다.
    var counts = (data && data.counts) || null;

    refs.detailTitle.textContent = '작업 ' + job.id + '번, ' + jobTypeLabel(job.type);
    refs.detailDesc.textContent = job.message || '';

    // --- 동작 버튼 -------------------------------------------------------
    clear(refs.detailActions);
    var isActive = job.status === 'running' || job.status === 'pending';
    var isResumable = job.status === 'paused' || job.status === 'pending';

    if (isActive) {
      refs.detailActions.appendChild(
        actionButton('취소', 'app-btn--secondary', function (button) {
          return confirmAndPost(
            button,
            job.id,
            'cancel',
            '작업을 취소할까요?',
            '진행 중인 항목까지만 마무리하고 중단합니다. 이미 삭제된 댓글은 되돌아오지 않습니다.',
            '작업 취소'
          );
        })
      );
    }
    if (isResumable) {
      refs.detailActions.appendChild(
        actionButton('재개', 'app-btn--primary', function (button) {
          return confirmAndPost(
            button,
            job.id,
            'resume',
            '작업을 재개할까요?',
            '아직 처리하지 않은 항목부터 이어서 실행합니다.',
            '재개'
          );
        })
      );
    }
    if (job.failed) {
      refs.detailActions.appendChild(
        actionButton('실패 항목만 재시도', 'app-btn--secondary', function (button) {
          return confirmAndPost(
            button,
            job.id,
            'retry-failed',
            '실패한 항목만 다시 시도할까요?',
            '실패한 ' + TG.formatNumber(job.failed) + '건으로 새 작업을 만들어 실행합니다.',
            '재시도 시작'
          );
        })
      );
    }

    // --- 본문 -------------------------------------------------------------
    clear(refs.detailBody);

    var kv = el('div', { className: 'app-kv' }, [
      kvItem('상태', TG.JOB_STATUS_LABEL[job.status] || job.status || '-'),
      kvItem('전체', TG.formatNumber(job.total || 0) + '건'),
      kvItem('처리', TG.formatNumber(job.done || 0) + '건'),
      kvItem('성공', TG.formatNumber(job.succeeded || 0) + '건'),
      kvItem('실패', TG.formatNumber(job.failed || 0) + '건'),
      kvItem('건너뜀', TG.formatNumber(job.skipped || 0) + '건'),
      kvItem('생성', TG.formatDateTime(job.created_at)),
      kvItem('시작', TG.formatDateTime(job.started_at)),
      kvItem('종료', TG.formatDateTime(job.finished_at)),
      kvItem('드라이런', job.params && job.params.dry_run ? '예' : '아니오')
    ]);
    refs.detailBody.appendChild(kv);

    if (counts && typeof counts === 'object' && !Array.isArray(counts)) {
      var countNodes = Object.keys(counts).map(function (key) {
        return kvItem(itemStatusLabel(key), TG.formatNumber(counts[key]) + '건');
      });
      if (countNodes.length) {
        refs.detailBody.appendChild(
          el('div', { className: 'app-kv', attrs: { style: 'margin-top:10px' } }, countNodes)
        );
      }
    }

    if (job.error) {
      refs.detailBody.appendChild(
        el('div', { attrs: { style: 'margin-top:12px' } }, [
          TG.errorState('작업 오류', job.error)
        ])
      );
    }

    if (job.backup_path) {
      refs.detailBody.appendChild(
        el('div', { className: 'app-row', attrs: { style: 'margin-top:12px' } }, [
          el('span', { className: 'app-muted app-text-sm', text: '백업 파일' }),
          el('span', { className: 'app-path', text: job.backup_path }),
          backupCell(job)
        ])
      );
    }

    // --- 항목 목록 --------------------------------------------------------
    refs.detailBody.appendChild(buildItemsSection(job.id));
  }

  function kvItem(key, value) {
    return el('div', { className: 'app-kv__item' }, [
      el('span', { className: 'app-kv__key', text: key }),
      el('span', { className: 'app-kv__val', text: value })
    ]);
  }

  function actionButton(label, variant, handler) {
    var button = el('button', {
      className: 'app-btn ' + variant + ' app-btn--sm',
      text: label,
      attrs: { type: 'button' }
    });
    button.addEventListener('click', function () {
      handler(button);
    });
    return button;
  }

  function confirmAndPost(button, jobId, action, title, body, confirmText) {
    return TG.confirmModal({
      title: title,
      body: body,
      confirmText: confirmText,
      danger: action === 'cancel'
    }).then(function (ok) {
      if (!ok) return;
      return TG.guard(button, function () {
        return TG.api
          .post('/api/jobs/' + encodeURIComponent(jobId) + '/' + action, {})
          .then(function (data) {
            TG.toast('요청을 처리했습니다.', 'success');
            var nextJobId = data && data.job_id;
            return loadList().then(function () {
              if (nextJobId && nextJobId !== jobId) selectJob(nextJobId);
              else loadDetail(jobId);
            });
          });
      }).catch(function () {});
    });
  }

  var ITEM_STATUS_LABEL = {
    pending: '대기',
    succeeded: '성공',
    failed: '실패',
    skipped: '건너뜀'
  };

  var ITEM_STATUS_TONE = {
    pending: 'gray',
    succeeded: 'green',
    failed: 'red',
    skipped: 'amber'
  };

  function itemStatusLabel(status) {
    return ITEM_STATUS_LABEL[status] || status;
  }

  function buildItemsSection(jobId) {
    var tbody = el('tbody', {});
    var pagerInfo = el('span', { className: 'app-pager__info', text: '-' });
    var prev = el('button', {
      className: 'app-btn app-btn--secondary app-btn--sm',
      text: '이전',
      attrs: { type: 'button' }
    });
    var next = el('button', {
      className: 'app-btn app-btn--secondary app-btn--sm',
      text: '다음',
      attrs: { type: 'button' }
    });

    var chips = el('div', { className: 'app-chips', attrs: { role: 'group', 'aria-label': '항목 상태 필터' } });
    [
      ['', '전체'],
      ['failed', '실패'],
      ['succeeded', '성공'],
      ['skipped', '건너뜀'],
      ['pending', '대기']
    ].forEach(function (pair) {
      var chip = el('button', {
        className: 'app-chip',
        text: pair[1],
        attrs: {
          type: 'button',
          'data-item-status': pair[0],
          'aria-pressed': state.itemStatus === pair[0] ? 'true' : 'false'
        }
      });
      chip.addEventListener('click', function () {
        state.itemStatus = pair[0];
        state.itemPage = 1;
        TG.qsa('.app-chip', chips).forEach(function (other) {
          other.setAttribute(
            'aria-pressed',
            other.getAttribute('data-item-status') === pair[0] ? 'true' : 'false'
          );
        });
        fetchItems();
      });
      chips.appendChild(chip);
    });

    prev.addEventListener('click', function () {
      if (state.itemPage <= 1) return;
      state.itemPage -= 1;
      fetchItems();
    });
    next.addEventListener('click', function () {
      state.itemPage += 1;
      fetchItems();
    });

    function fetchItems() {
      clear(tbody);
      tbody.appendChild(TG.tableMessageRow(ITEM_COLSPAN, '항목을 불러오는 중입니다.'));

      var query = TG.buildQuery({
        status: state.itemStatus,
        page: state.itemPage,
        size: state.itemSize
      });
      return TG.api
        .get('/api/jobs/' + encodeURIComponent(jobId) + '/items?' + query, { silent: true })
        .then(function (data) {
          var items = (data && data.items) || [];
          var total = (data && data.total) || 0;
          clear(tbody);

          if (!items.length) {
            tbody.appendChild(TG.tableMessageRow(ITEM_COLSPAN, '표시할 항목이 없습니다.'));
          } else {
            items.forEach(function (item) {
              tbody.appendChild(
                el('tr', {}, [
                  el('td', { className: 'app-num', text: String(item.comment_id) }),
                  el('td', {}, [
                    TG.badge(itemStatusLabel(item.status), ITEM_STATUS_TONE[item.status] || 'gray')
                  ]),
                  el('td', { className: 'app-num', text: TG.formatNumber(item.attempts || 0) }),
                  el('td', {
                    className: 'app-num',
                    text: item.http_status === null || item.http_status === undefined
                      ? '-'
                      : String(item.http_status)
                  }),
                  el('td', {}, [
                    el('span', {
                      className: 'app-truncate app-cell-content',
                      text: item.message || '-',
                      title: item.message || ''
                    })
                  ])
                ])
              );
            });
          }

          var pages = Math.max(1, Math.ceil(total / state.itemSize));
          pagerInfo.textContent = total
            ? state.itemPage + ' / ' + pages + ' 페이지, 총 ' + TG.formatNumber(total) + '건'
            : '표시할 항목이 없습니다.';
          prev.disabled = state.itemPage <= 1;
          next.disabled = state.itemPage >= pages;
        })
        .catch(function () {
          clear(tbody);
          tbody.appendChild(TG.tableMessageRow(ITEM_COLSPAN, '항목을 불러오지 못했습니다.'));
        });
    }

    var section = el('div', { attrs: { style: 'margin-top:18px' } }, [
      el('div', { className: 'app-card__head', attrs: { style: 'margin-bottom:10px' } }, [
        el('div', {}, [
          el('h3', { className: 'app-card__title', text: '작업 항목' }),
          el('p', { className: 'app-card__desc', text: '건별 처리 결과와 실패 사유입니다.' })
        ]),
        el('div', { className: 'app-card__actions' }, [chips])
      ]),
      el('div', { className: 'app-table-wrap' }, [
        el('table', { className: 'app-table' }, [
          el('thead', {}, [
            el('tr', {}, [
              el('th', { text: '댓글 번호', attrs: { scope: 'col', class: 'app-num' } }),
              el('th', { text: '상태', attrs: { scope: 'col' } }),
              el('th', { text: '시도', attrs: { scope: 'col', class: 'app-num' } }),
              el('th', { text: 'HTTP', attrs: { scope: 'col', class: 'app-num' } }),
              el('th', { text: '메시지', attrs: { scope: 'col' } })
            ])
          ]),
          tbody
        ])
      ]),
      el('div', { className: 'app-pager' }, [
        pagerInfo,
        el('div', { className: 'app-pager__nav' }, [prev, next])
      ])
    ]);

    // 상세를 열 때 항목도 함께 가져온다.
    fetchItems();
    return section;
  }

  // ==========================================================================
  // 실시간 갱신
  // ==========================================================================

  function applyLiveUpdate(payload) {
    if (!payload) return;
    var entry = rowIndex.get(payload.job_id);
    if (!entry) return;

    applyProgress(entry.bar, entry.meta, payload);
    clear(entry.statusCell);
    entry.statusCell.appendChild(statusBadge(payload.status));
    entry.succeeded.textContent = TG.formatNumber(payload.succeeded || 0);
    entry.skipped.textContent = TG.formatNumber(payload.skipped || 0);

    clear(entry.failed);
    if (payload.failed) entry.failed.appendChild(TG.badge(TG.formatNumber(payload.failed), 'red'));
    else entry.failed.appendChild(el('span', { text: '0' }));

    if (payload.status && ['completed', 'failed', 'cancelled'].indexOf(payload.status) >= 0) {
      entry.finished.textContent = TG.formatDateTime(payload.updated_at);
      // 열려 있는 상세가 방금 끝난 작업이면 다시 읽어 최종 결과를 보여준다.
      if (state.selectedJobId === payload.job_id) loadDetail(payload.job_id);
    }
  }

  TG.subscribeJobsStream({
    onProgress: applyLiveUpdate,
    onDone: applyLiveUpdate,
    onError: function (info) {
      if (info && info.reason === 'disconnected') {
        TG.toast('실시간 갱신 연결이 끊겼습니다. 새로 고침으로 최신 상태를 확인하세요.', 'info');
      }
    }
  });

  // ==========================================================================
  // 이벤트
  // ==========================================================================

  refs.typeChips.addEventListener('click', function (event) {
    var chip = event.target.closest('.app-chip');
    if (!chip) return;
    state.type = chip.getAttribute('data-type') || '';
    state.page = 1;
    TG.qsa('.app-chip', refs.typeChips).forEach(function (other) {
      other.setAttribute(
        'aria-pressed',
        (other.getAttribute('data-type') || '') === state.type ? 'true' : 'false'
      );
    });
    loadList();
  });

  refs.refresh.addEventListener('click', function () {
    TG.guard(refs.refresh, function () {
      return loadList().then(function () {
        if (state.selectedJobId) return loadDetail(state.selectedJobId);
      });
    });
  });

  refs.prev.addEventListener('click', function () {
    if (state.page <= 1) return;
    state.page -= 1;
    loadList();
  });

  refs.next.addEventListener('click', function () {
    state.page += 1;
    loadList();
  });

  loadList();
})();
