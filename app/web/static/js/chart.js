// app/web/static/js/chart.js
//
// 외부 차트 라이브러리 없이 인라인 SVG 로 막대 차트를 그린다.
// 색은 CSS 변수(--app-chart-*)를 getComputedStyle 로 읽어 쓰므로 디자인 토큰과 항상 일치한다.
// 컨테이너 너비에 맞춰 다시 그리며, 라벨은 겹치지 않도록 솎아낸다.
//
// 공개 함수
//   TGChart.renderBarChart(container, data, options)
//   TGChart.renderHorizontalBars(container, data, options)
//
// data 형식: [{ label: '2026-08-08 20:00', value: 312 }, ...]

(function (global) {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    value = (value || '').trim();
    return value || fallback;
  }

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, String(attrs[key]));
    });
    return node;
  }

  function svgText(content, attrs) {
    var node = svgEl('text', attrs);
    // 사용자 입력(닉네임)이 들어올 수 있으므로 textContent 로만 넣는다.
    node.textContent = content;
    return node;
  }

  function formatNumber(value) {
    var num = Number(value);
    if (!isFinite(num)) return '0';
    return num.toLocaleString('ko-KR');
  }

  /** 축 최대값을 사람이 읽기 좋은 값으로 올림한다. */
  function niceMax(value) {
    if (!isFinite(value) || value <= 0) return 1;
    var exponent = Math.floor(Math.log10(value));
    var magnitude = Math.pow(10, exponent);
    var normalized = value / magnitude;
    var step;
    if (normalized <= 1) step = 1;
    else if (normalized <= 2) step = 2;
    else if (normalized <= 2.5) step = 2.5;
    else if (normalized <= 5) step = 5;
    else step = 10;
    return step * magnitude;
  }

  /** SVG 글자 폭을 보수적으로 추정한다. 한글은 넓게 잡는다. */
  function estimateTextWidth(text, fontSize) {
    var width = 0;
    for (var i = 0; i < text.length; i += 1) {
      var code = text.charCodeAt(i);
      width += code > 0x2e00 ? fontSize : fontSize * 0.56;
    }
    return width;
  }

  /** 폭에 맞게 문자열을 자른다. 잘리면 뒤에 마침표 세 개를 붙인다. */
  function fitText(text, maxWidth, fontSize) {
    if (estimateTextWidth(text, fontSize) <= maxWidth) return text;
    var result = text;
    while (result.length > 1 && estimateTextWidth(result + '...', fontSize) > maxWidth) {
      result = result.slice(0, -1);
    }
    return result + '...';
  }

  // ==========================================================================
  // 툴팁
  // ==========================================================================

  function ensureTooltip(container) {
    var tip = container.querySelector('.app-chart__tooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'app-chart__tooltip';
      tip.hidden = true;
      var labelNode = document.createElement('span');
      labelNode.className = 'app-chart__tooltip-label';
      var valueNode = document.createElement('span');
      valueNode.className = 'app-chart__tooltip-value';
      tip.appendChild(labelNode);
      tip.appendChild(valueNode);
      container.appendChild(tip);
    }
    return tip;
  }

  function showTooltip(container, x, y, label, value) {
    var tip = ensureTooltip(container);
    tip.firstChild.textContent = label;
    tip.lastChild.textContent = value;
    tip.hidden = false;
    // 좌우 화면 밖으로 나가지 않도록 위치를 보정한다.
    var half = tip.offsetWidth / 2;
    var left = Math.max(half + 2, Math.min(container.clientWidth - half - 2, x));
    tip.style.left = left + 'px';
    tip.style.top = Math.max(tip.offsetHeight + 2, y - 8) + 'px';
  }

  function hideTooltip(container) {
    var tip = container.querySelector('.app-chart__tooltip');
    if (tip) tip.hidden = true;
  }

  // ==========================================================================
  // 세로 막대 차트
  // ==========================================================================

  /**
   * 세로 막대 차트를 그린다.
   *
   * options
   *   height      : 차트 높이(px). 기본 240
   *   valueUnit   : 툴팁과 축에 붙일 단위. 기본 '건'
   *   ariaLabel   : 접근성 설명
   *   labelFormat : function(label) -> x축에 표시할 문자열
   */
  function renderBarChart(container, data, options) {
    if (!container) return;
    var config = options || {};
    var series = (data || []).map(function (row) {
      return {
        label: String(row.label === undefined || row.label === null ? '' : row.label),
        value: Number(row.value) || 0
      };
    });

    container.classList.add('app-chart');
    bindResize(container, function () {
      drawBarChart(container, series, config);
    });
    drawBarChart(container, series, config);
  }

  function drawBarChart(container, series, config) {
    var existing = container.querySelector('svg');
    if (existing) existing.remove();
    hideTooltip(container);

    var width = Math.max(container.clientWidth || 0, 320);
    var height = config.height || 240;
    var unit = config.valueUnit || '건';
    var padTop = 14;
    var padBottom = 30;
    var padRight = 8;

    if (!series.length) {
      container.appendChild(
        buildEmptySvg(width, height, config.emptyText || '표시할 데이터가 없습니다.')
      );
      return;
    }

    var maxValue = series.reduce(function (acc, row) {
      return Math.max(acc, row.value);
    }, 0);
    var axisMax = niceMax(maxValue || 1);
    var tickCount = 4;

    // y축 라벨 폭을 실제 최대 눈금 문자열로 계산한다.
    var padLeft = Math.max(34, estimateTextWidth(formatNumber(axisMax), 11) + 12);

    var plotW = Math.max(10, width - padLeft - padRight);
    var plotH = Math.max(10, height - padTop - padBottom);

    var svg = svgEl('svg', {
      width: width,
      height: height,
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label':
        config.ariaLabel ||
        ('막대 차트, 구간 ' + series.length + '개, 최대 ' + formatNumber(maxValue) + unit)
    });

    var barColor = cssVar('--app-chart-bar', '#2c5ce6');

    // 그리드와 y축 눈금
    for (var t = 0; t <= tickCount; t += 1) {
      var ratio = t / tickCount;
      var y = padTop + plotH - plotH * ratio;
      svg.appendChild(
        svgEl('line', {
          class: 'app-chart__grid',
          x1: padLeft,
          y1: y,
          x2: padLeft + plotW,
          y2: y
        })
      );
      svg.appendChild(
        svgText(formatNumber(Math.round(axisMax * ratio)), {
          class: 'app-chart__tick',
          x: padLeft - 7,
          y: y + 3.5,
          'text-anchor': 'end'
        })
      );
    }

    // 막대 배치
    var slot = plotW / series.length;
    var barW = Math.max(2, Math.min(slot * 0.68, 44));

    // x축 라벨 솎아내기: 라벨 하나가 차지하는 폭을 기준으로 간격을 정한다.
    var sampleLabel = formatLabel(series[0].label, config);
    var labelWidth = estimateTextWidth(sampleLabel, 11) + 14;
    var labelStep = Math.max(1, Math.ceil(labelWidth / slot));

    series.forEach(function (row, index) {
      var centerX = padLeft + slot * index + slot / 2;
      var barH = axisMax > 0 ? (row.value / axisMax) * plotH : 0;
      if (row.value > 0) barH = Math.max(barH, 2);
      var barY = padTop + plotH - barH;

      var rect = svgEl('rect', {
        class: 'app-chart__bar',
        x: centerX - barW / 2,
        y: barY,
        width: barW,
        height: barH,
        rx: Math.min(3, barW / 2),
        fill: barColor
      });
      svg.appendChild(rect);

      // 마우스를 정확히 막대 위에 올리지 않아도 반응하도록 넓은 히트 영역을 둔다.
      var hit = svgEl('rect', {
        class: 'app-chart__hit',
        x: padLeft + slot * index,
        y: padTop,
        width: slot,
        height: plotH
      });
      hit.addEventListener('mouseenter', function () {
        rect.classList.add('app-chart__bar--active');
        showTooltip(
          container,
          centerX,
          Math.max(barY, padTop + 12),
          formatLabel(row.label, config, true),
          formatNumber(row.value) + unit
        );
      });
      hit.addEventListener('mouseleave', function () {
        rect.classList.remove('app-chart__bar--active');
        hideTooltip(container);
      });
      svg.appendChild(hit);

      // 라벨은 첫 칸부터 일정 간격으로만 그린다.
      if (index % labelStep === 0) {
        svg.appendChild(
          svgText(formatLabel(row.label, config), {
            class: 'app-chart__label',
            x: centerX,
            y: height - 10,
            'text-anchor': 'middle'
          })
        );
      }
    });

    // x축 기준선
    svg.appendChild(
      svgEl('line', {
        class: 'app-chart__grid',
        x1: padLeft,
        y1: padTop + plotH,
        x2: padLeft + plotW,
        y2: padTop + plotH
      })
    );

    container.insertBefore(svg, container.firstChild);
  }

  /**
   * 히스토그램 라벨을 축용으로 줄인다.
   * '2026-08-08 20:00' 은 축에서 '08-08 20시', 툴팁에서 원문 그대로 보여준다.
   */
  function formatLabel(label, config, full) {
    if (config && typeof config.labelFormat === 'function') {
      return String(config.labelFormat(label, !!full));
    }
    if (full) return label;
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(label);
    if (m) return m[2] + '-' + m[3] + ' ' + m[4] + '시';
    return label;
  }

  // ==========================================================================
  // 가로 막대 (상위 작성자 등 순위 표시)
  // ==========================================================================

  /**
   * 가로 막대를 그린다. 항목 이름이 길어도 폭에 맞춰 잘라 표시한다.
   *
   * options
   *   rowHeight : 한 줄 높이. 기본 30
   *   valueUnit : 값 단위. 기본 '건'
   *   maxItems  : 표시 개수 상한. 기본 10
   */
  function renderHorizontalBars(container, data, options) {
    if (!container) return;
    var config = options || {};
    var limit = config.maxItems || 10;
    var series = (data || []).slice(0, limit).map(function (row) {
      return {
        label: String(row.label === undefined || row.label === null ? '' : row.label),
        value: Number(row.value) || 0
      };
    });

    container.classList.add('app-chart');
    bindResize(container, function () {
      drawHorizontalBars(container, series, config);
    });
    drawHorizontalBars(container, series, config);
  }

  function drawHorizontalBars(container, series, config) {
    var existing = container.querySelector('svg');
    if (existing) existing.remove();
    hideTooltip(container);

    var width = Math.max(container.clientWidth || 0, 280);
    var rowH = config.rowHeight || 30;
    var unit = config.valueUnit || '건';

    if (!series.length) {
      container.appendChild(
        buildEmptySvg(width, rowH * 3, config.emptyText || '표시할 데이터가 없습니다.')
      );
      return;
    }

    var height = series.length * rowH + 6;
    var maxValue = series.reduce(function (acc, row) {
      return Math.max(acc, row.value);
    }, 0) || 1;

    // 이름 열과 값 열 폭을 전체 너비에 비례해 정한다.
    var nameW = Math.max(80, Math.min(170, Math.round(width * 0.32)));
    var valueW = Math.max(46, estimateTextWidth(formatNumber(maxValue) + unit, 12) + 8);
    var trackX = nameW + 10;
    var trackW = Math.max(20, width - trackX - valueW - 6);

    var svg = svgEl('svg', {
      width: width,
      height: height,
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label': config.ariaLabel || ('가로 막대 차트, 항목 ' + series.length + '개')
    });

    var barColor = cssVar('--app-chart-bar', '#2c5ce6');
    var trackColor = cssVar('--app-chart-track', '#eef0f4');

    series.forEach(function (row, index) {
      var top = index * rowH + 3;
      var barH = Math.min(14, rowH - 12);
      var barY = top + (rowH - 6 - barH) / 2;
      var ratio = row.value / maxValue;
      var barW = Math.max(row.value > 0 ? 3 : 0, trackW * ratio);

      svg.appendChild(
        svgText(fitText(row.label, nameW, 12.5), {
          class: 'app-chart__name',
          x: 0,
          y: barY + barH / 2 + 4.5
        })
      );

      svg.appendChild(
        svgEl('rect', {
          x: trackX,
          y: barY,
          width: trackW,
          height: barH,
          rx: barH / 2,
          fill: trackColor
        })
      );

      var bar = svgEl('rect', {
        class: 'app-chart__bar',
        x: trackX,
        y: barY,
        width: barW,
        height: barH,
        rx: barH / 2,
        fill: barColor
      });
      svg.appendChild(bar);

      svg.appendChild(
        svgText(formatNumber(row.value) + unit, {
          class: 'app-chart__value',
          x: width,
          y: barY + barH / 2 + 4.5,
          'text-anchor': 'end'
        })
      );

      var hit = svgEl('rect', {
        class: 'app-chart__hit',
        x: 0,
        y: top,
        width: width,
        height: rowH
      });
      hit.addEventListener('mouseenter', function () {
        bar.classList.add('app-chart__bar--active');
        showTooltip(
          container,
          trackX + Math.min(trackW, barW),
          barY,
          row.label,
          formatNumber(row.value) + unit
        );
      });
      hit.addEventListener('mouseleave', function () {
        bar.classList.remove('app-chart__bar--active');
        hideTooltip(container);
      });
      svg.appendChild(hit);
    });

    container.insertBefore(svg, container.firstChild);
  }

  // ==========================================================================
  // 공통
  // ==========================================================================

  function buildEmptySvg(width, height, message) {
    var svg = svgEl('svg', {
      width: width,
      height: height,
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-label': message
    });
    svg.appendChild(
      svgText(message, {
        class: 'app-chart__label',
        x: width / 2,
        y: height / 2,
        'text-anchor': 'middle'
      })
    );
    return svg;
  }

  /**
   * 컨테이너 폭이 바뀌면 다시 그린다.
   * 같은 컨테이너에 여러 번 호출되어도 관찰자는 하나만 유지한다.
   */
  function bindResize(container, redraw) {
    if (container.__tgRedraw) {
      container.__tgRedraw = redraw;
      return;
    }
    container.__tgRedraw = redraw;

    var lastWidth = container.clientWidth;
    var timer = null;
    var run = function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        var current = container.clientWidth;
        if (current === lastWidth) return;
        lastWidth = current;
        if (container.__tgRedraw) container.__tgRedraw();
      }, 120);
    };

    if (typeof ResizeObserver === 'function') {
      var observer = new ResizeObserver(run);
      observer.observe(container);
    } else {
      global.addEventListener('resize', run);
    }
  }

  global.TGChart = {
    renderBarChart: renderBarChart,
    renderHorizontalBars: renderHorizontalBars
  };
})(window);
