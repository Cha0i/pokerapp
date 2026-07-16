// ==UserScript==
// @name         PokerOdds Tagged Bridge
// @namespace    http://tampermonkey.net/
// @version      2.4
// @description  Forward poker console messages to local PokerOdds bridge
// @downloadURL  http://127.0.0.1:5000/tampermonkey-bridge.user.js
// @updateURL    http://127.0.0.1:5000/tampermonkey-bridge.user.js
// @match        *://casino.org/*
// @match        *://*.casino.org/*
// @match        *://unibet.nl/*
// @match        *://*.unibet.nl/*
// @match        *://*.relaxg.com/kenobi/clients/unibet/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// ==/UserScript==

(function () {
    'use strict';

    var SCRIPT_VERSION = '2.4';
    var BRIDGE_URLS = ['http://127.0.0.1:5000/log', 'http://localhost:5000/log'];
    var currentBridgeUrlIndex = 0;
    var BRIDGE_TAG = 'TM_BRIDGE:';
    var RAW_TAG = '[RAW_CONSOLE]';
    var DEFAULT_RELAX_HERO_NAME = 'xtlx';
    var ENABLE_RAW_MIRROR = isDiscoveryHost();
    var ENABLE_STRATEGY_EVENT_MIRROR = true;
    var REHOOK_INTERVAL_MS = 1500;
    var lastSentKey = null;
    var HOOK_FLAG = '__tmPokerBridgeHooked__';
    var pendingLines = [];
    var flushTimer = null;
    var flushing = false;
    var retryDelayMs = 200;
    var MAX_QUEUE_SIZE = 5000;
    var MAX_DISCOVERY_TEXT_LENGTH = 2600;
    var MAX_BINARY_PREVIEW_BYTES = 1536;
    var bridgeTransportState = null;
    var originalConsole = window.console;
    var originalConsoleInfo = originalConsole && originalConsole.info;
    var originalConsoleWarn = originalConsole && originalConsole.warn;

    function localDiagnostic(level, message) {
        var logger = level === 'warn' ? originalConsoleWarn : originalConsoleInfo;
        if (typeof logger !== 'function') {
            return;
        }
        try {
            logger.call(originalConsole, '[PokerOdds Bridge v' + SCRIPT_VERSION + '] ' + message);
        } catch (_) {}
    }

    function reportTransportState(state, detail) {
        if (bridgeTransportState === state) {
            return;
        }
        bridgeTransportState = state;
        localDiagnostic(state === 'connected' ? 'info' : 'warn', detail);
    }

    function currentSiteKey() {
        var host = window.location && window.location.hostname ? window.location.hostname.toLowerCase() : '';
        if (host === 'unibet.nl' || host === 'www.unibet.nl') {
            return 'unibet_nl_pokerwebclient';
        }
        if (host.slice(-10) === '.unibet.nl') {
            return 'unibet_nl_pokerwebclient';
        }
        if (
            (host === 'relaxg.com' || host.slice(-11) === '.relaxg.com') &&
            window.location.pathname.toLowerCase().indexOf('/kenobi/clients/unibet/') === 0
        ) {
            return 'unibet_nl_pokerwebclient';
        }
        if (host === 'casino.org' || host.slice(-11) === '.casino.org') {
            return 'casino_org_replaypoker';
        }
        return 'unknown';
    }

    function isDiscoveryHost() {
        if (currentSiteKey() !== 'unibet_nl_pokerwebclient') {
            return false;
        }
        var host = window.location && window.location.hostname ? window.location.hostname.toLowerCase() : '';
        var path = window.location && window.location.pathname ? window.location.pathname.toLowerCase() : '';
        return (
            (host === 'relaxg.com' || host.slice(-11) === '.relaxg.com') &&
            path.indexOf('/kenobi/clients/unibet/') === 0
        );
    }

    function isUnibetLauncherPage() {
        var host = window.location && window.location.hostname ? window.location.hostname.toLowerCase() : '';
        var path = window.location && window.location.pathname ? window.location.pathname.toLowerCase() : '';
        return (
            (host === 'unibet.nl' || host === 'www.unibet.nl' || host.slice(-10) === '.unibet.nl') &&
            path.indexOf('/play/pokerwebclient') === 0
        );
    }

    function shouldDiscoverIframes() {
        return isDiscoveryHost() || isUnibetLauncherPage();
    }

    function rawMirrorEnabled() {
        return ENABLE_RAW_MIRROR || isDiscoveryHost();
    }

    function pageContext() {
        var locationText = '[unknown-page]';
        try {
            locationText = window.location.origin + window.location.pathname;
        } catch (_) {}
        var frameText = 'top';
        try {
            frameText = window.self === window.top ? 'top' : 'frame';
        } catch (_) {
            frameText = 'frame';
        }
        return locationText + ' ' + frameText;
    }

    var handState = {
        handId: null,
        activeSeats: {},
        activeCount: null,
        boardCount: 0,
        holeSentForHand: false,
        heroUserId: null,
        heroSeatId: null,
        heroSittingOut: null
    };
    var relaxHandState = {
        handId: null,
        heroSeatId: null,
        holeKey: null,
        holeCards: null,
        boardKey: null,
        boardCards: [],
        playersCount: null,
        heroSittingOut: null,
        heroFolded: null,
        pot: null,
        toCall: null,
        minimumRaise: null,
        heroTurn: null,
        resetSent: false
    };
    var relaxDecoderReported = false;

    function currentBridgeUrl() {
        return BRIDGE_URLS[currentBridgeUrlIndex % BRIDGE_URLS.length];
    }

    function rotateBridgeUrl() {
        currentBridgeUrlIndex = (currentBridgeUrlIndex + 1) % BRIDGE_URLS.length;
    }

    function scheduleFlush(delayMs) {
        if (flushTimer !== null) {
            return;
        }
        flushTimer = window.setTimeout(function () {
            flushTimer = null;
            flushQueue();
        }, delayMs);
    }

    function enqueueLine(line) {
        if (!line) {
            return;
        }
        pendingLines.push(line);
        if (pendingLines.length > MAX_QUEUE_SIZE) {
            pendingLines.splice(0, pendingLines.length - MAX_QUEUE_SIZE);
        }
        scheduleFlush(0);
    }

    function flushQueue() {
        if (flushing || pendingLines.length === 0) {
            return;
        }
        flushing = true;
        var line = pendingLines[0];
        var url = currentBridgeUrl();

        GM_xmlhttpRequest({
            method: 'POST',
            url: url,
            timeout: 5000,
            headers: {
                'Content-Type': 'text/plain'
            },
            data: line,
            onload: function (res) {
                flushing = false;
                if (res.status === 200) {
                    reportTransportState('connected', 'connected to ' + url);
                    pendingLines.shift();
                    retryDelayMs = 200;
                    if (pendingLines.length > 0) {
                        scheduleFlush(0);
                    }
                    return;
                }

                reportTransportState('http-error', 'bridge returned HTTP ' + res.status + ' at ' + url);
                rotateBridgeUrl();
                retryDelayMs = Math.min(5000, retryDelayMs * 2);
                scheduleFlush(retryDelayMs);
            },
            onerror: function (error) {
                flushing = false;
                reportTransportState('request-error', 'could not reach ' + url + ': ' + summarizeBody(error));
                rotateBridgeUrl();
                retryDelayMs = Math.min(5000, retryDelayMs * 2);
                scheduleFlush(retryDelayMs);
            },
            ontimeout: function () {
                flushing = false;
                reportTransportState('timeout', 'request timed out at ' + url);
                rotateBridgeUrl();
                retryDelayMs = Math.min(5000, retryDelayMs * 2);
                scheduleFlush(retryDelayMs);
            }
        });
    }

    function sendLine(line) {
        enqueueLine(line);
    }

    function sendRawLine(source, level, args) {
        if (!rawMirrorEnabled()) {
            return;
        }
        sendLine(formatRawLine(source, level, args));
    }

    function formatRawLine(source, level, args) {
        return redactSensitiveText(
            RAW_TAG +
            ' [site:' + currentSiteKey() + ']' +
            ' [page:' + pageContext() + ']' +
            ' [' + source + ':' + level + '] ' +
            compactArgs(args)
        );
    }

    function redactSensitiveText(value) {
        return String(value)
            .replace(/(<auth\b[^>]*>)[\s\S]*?(<\/auth>)/gi, '$1[redacted]$2')
            .replace(/(&quot;(?:token|relaxtoken|ticket|password|access_token)&quot;\s*:\s*&quot;)[\s\S]*?(&quot;)/gi, '$1[redacted]$2')
            .replace(/("(?:token|relaxtoken|ticket|password|access_token)"\s*:\s*")[^"]*(")/gi, '$1[redacted]$2')
            .replace(/([?&](?:ticket|token|access_token)=)[^&\s|]+/gi, '$1[redacted]')
            .replace(/(authorization\s*[:=]\s*bearer\s+)[^\s|]+/gi, '$1[redacted]');
    }

    function isPokerStrategyCandidate(value, seen) {
        if (!value || typeof value !== 'object') {
            return false;
        }
        if (seen.indexOf(value) !== -1) {
            return false;
        }
        seen.push(value);

        if (Object.prototype.hasOwnProperty.call(value, 'action')) {
            return true;
        }
        if (Array.isArray(value.updates)) {
            return true;
        }
        if (
            Object.prototype.hasOwnProperty.call(value, 'handId') &&
            (
                Object.prototype.hasOwnProperty.call(value, 'players') ||
                Object.prototype.hasOwnProperty.call(value, 'seats') ||
                Object.prototype.hasOwnProperty.call(value, 'tableId')
            )
        ) {
            return true;
        }

        var keys = Object.keys(value);
        for (var i = 0; i < keys.length; i += 1) {
            var child = value[keys[i]];
            if (child && typeof child === 'object' && isPokerStrategyCandidate(child, seen)) {
                return true;
            }
        }
        return false;
    }

    function sendStrategyEventLines(source, args) {
        if (!ENABLE_STRATEGY_EVENT_MIRROR || ENABLE_RAW_MIRROR) {
            return;
        }
        for (var i = 0; i < args.length; i += 1) {
            var arg = args[i];
            if (!arg || typeof arg !== 'object') {
                continue;
            }
            if (!isPokerStrategyCandidate(arg, [])) {
                continue;
            }
            var text = safeStringify(arg);
            if (text.length > 2200) {
                text = text.slice(0, 2200) + '...[truncated]';
            }
            sendLine(RAW_TAG + ' [site:' + currentSiteKey() + '] [' + source + ':POKER_EVENT] event | ' + text);
        }
    }

    function safeStringify(value) {
        var cache = [];
        try {
            return JSON.stringify(value, function (_key, val) {
                if (typeof val === 'object' && val !== null) {
                    if (cache.indexOf(val) !== -1) {
                        return '[Circular]';
                    }
                    cache.push(val);
                }
                return val;
            });
        } catch (_) {
            try {
                return String(value);
            } catch (__ ) {
                return '[Unserializable]';
            }
        }
    }

    function compactArgs(args) {
        var parts = [];
        for (var i = 0; i < args.length; i += 1) {
            var arg = args[i];
            if (typeof arg === 'string') {
                parts.push(arg);
            } else {
                parts.push(safeStringify(arg));
            }
        }
        var line = parts.join(' | ');
        if (line.length > 2200) {
            return line.slice(0, 2200) + '...[truncated]';
        }
        return line;
    }

    function sendDebug(message) {
        sendLine('[BRIDGE_DEBUG] [v' + SCRIPT_VERSION + '] [page:' + pageContext() + '] ' + message);
    }

    function truncateDiscoveryText(text) {
        if (typeof text !== 'string') {
            text = String(text);
        }
        if (text.length > MAX_DISCOVERY_TEXT_LENGTH) {
            return text.slice(0, MAX_DISCOVERY_TEXT_LENGTH) + '...[truncated ' + text.length + ' chars]';
        }
        return text;
    }

    function requestUrl(input) {
        if (typeof input === 'string') {
            return input;
        }
        if (input && typeof input.url === 'string') {
            return input.url;
        }
        try {
            return String(input);
        } catch (_) {
            return '[unknown-url]';
        }
    }

    function requestMethod(input, init) {
        if (init && typeof init.method === 'string') {
            return init.method.toUpperCase();
        }
        if (input && typeof input.method === 'string') {
            return input.method.toUpperCase();
        }
        return 'GET';
    }

    function summarizeBody(value) {
        if (value === undefined || value === null) {
            return '-';
        }
        if (typeof value === 'string') {
            return 'text(' + value.length + '): ' + truncateDiscoveryText(value);
        }
        if (typeof ArrayBuffer !== 'undefined' && value instanceof ArrayBuffer) {
            return binaryPreview('ArrayBuffer', new Uint8Array(value));
        }
        if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(value)) {
            return binaryPreview(
                value.constructor.name,
                new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
            );
        }
        if (typeof Blob !== 'undefined' && value instanceof Blob) {
            return 'Blob(type=' + (value.type || '-') + ', size=' + value.size + ')';
        }
        if (typeof URLSearchParams !== 'undefined' && value instanceof URLSearchParams) {
            return 'URLSearchParams: ' + truncateDiscoveryText(value.toString());
        }
        if (typeof FormData !== 'undefined' && value instanceof FormData) {
            var keys = [];
            try {
                value.forEach(function (_fieldValue, key) {
                    keys.push(key);
                });
            } catch (_) {}
            return 'FormData(keys=' + keys.join(',') + ')';
        }
        if (typeof value === 'object') {
            return truncateDiscoveryText(safeStringify(value));
        }
        return truncateDiscoveryText(String(value));
    }

    function summarizeWebSocketBody(value) {
        if (typeof value !== 'string') {
            return summarizeBody(value);
        }
        var redacted = redactSensitiveText(value);
        return 'text(' + value.length + '): ' + truncateDiscoveryText(redacted);
    }

    function binaryPreview(label, bytes) {
        var previewLength = Math.min(bytes.length, MAX_BINARY_PREVIEW_BYTES);
        var binary = '';
        for (var i = 0; i < previewLength; i += 1) {
            binary += String.fromCharCode(bytes[i]);
        }
        var encoded = '';
        try {
            encoded = window.btoa(binary);
        } catch (_) {
            encoded = '[base64-failed]';
        }
        var suffix = bytes.length > previewLength ? '...[truncated]' : '';
        return label + '(' + bytes.length + ') base64:' + encoded + suffix;
    }

    function sendDiscoveryLine(source, level, args) {
        var launcherLevel = level === 'IFRAME' || level === 'WINDOW_MESSAGE';
        if (!isDiscoveryHost() && !(launcherLevel && isUnibetLauncherPage())) {
            return;
        }
        sendLine(formatRawLine(source, level, args));
    }

    function sendPayload(payload) {
        if (!payload) {
            return;
        }
        var key = JSON.stringify(payload);
        if (key === lastSentKey) {
            return;
        }
        lastSentKey = key;
        sendLine(BRIDGE_TAG + key);
    }

    function looksTaggedLine(value) {
        return typeof value === 'string' && value.trim().indexOf(BRIDGE_TAG) === 0;
    }

    function normalizeCard(card) {
        if (typeof card !== 'string') {
            return null;
        }
        var cleaned = card.trim();
        if (!/^[2-9TJQKA][cdhs]$/i.test(cleaned)) {
            return null;
        }
        return cleaned[0].toUpperCase() + cleaned[1].toLowerCase();
    }

    function isValidCardCode(value) {
        return typeof value === 'string' && /^[2-9TJQKA][cdhs]$/i.test(value.trim());
    }

    function normalizeCardArray(cards, maxCount) {
        if (!Array.isArray(cards) || cards.length > maxCount) {
            return null;
        }
        var out = [];
        for (var i = 0; i < cards.length; i += 1) {
            var normalized = normalizeCard(cards[i]);
            if (!normalized) {
                return null;
            }
            out.push(normalized);
        }
        return out;
    }

    function normalizeCompactCards(value, expectedCount) {
        if (typeof value !== 'string' || value.length !== expectedCount * 2) {
            return null;
        }
        var cards = [];
        for (var i = 0; i < value.length; i += 2) {
            var card = normalizeCard(value.slice(i, i + 2));
            if (!card) {
                return null;
            }
            cards.push(card);
        }
        return cards;
    }

    function parseRelaxFrameBodies(frameText) {
        if (typeof frameText !== 'string' || frameText.indexOf('<body') === -1) {
            return [];
        }
        var matches = frameText.match(/<body\b[^>]*>[\s\S]*?<\/body>/gi);
        if (!matches) {
            return [];
        }
        var bodies = [];
        for (var i = 0; i < matches.length; i += 1) {
            try {
                var documentValue = new DOMParser().parseFromString(matches[i], 'application/xml');
                var bodyNode = documentValue.getElementsByTagName('body')[0];
                if (!bodyNode || !bodyNode.textContent) {
                    continue;
                }
                var parsed = JSON.parse(bodyNode.textContent);
                if (parsed && typeof parsed === 'object') {
                    bodies.push(parsed);
                }
            } catch (_) {}
        }
        return bodies;
    }

    function relaxActivePlayers(states) {
        if (!Array.isArray(states)) {
            return null;
        }
        var active = 0;
        for (var i = 0; i < states.length; i += 1) {
            if (states[i] === 1) {
                active += 1;
            }
        }
        if (active <= 0) {
            return null;
        }
        return Math.max(2, Math.min(10, active));
    }

    function relaxPotSize(compactTable) {
        if (!Array.isArray(compactTable)) {
            return null;
        }
        var total = 0;
        var found = false;
        var bets = Array.isArray(compactTable[3]) ? compactTable[3] : [];
        for (var i = 0; i < bets.length; i += 1) {
            if (typeof bets[i] === 'number' && bets[i] >= 0) {
                total += bets[i];
                found = true;
            }
        }
        var pots = Array.isArray(compactTable[4]) ? compactTable[4] : [];
        for (var p = 0; p < pots.length; p += 1) {
            if (Array.isArray(pots[p]) && typeof pots[p][0] === 'number' && pots[p][0] >= 0) {
                total += pots[p][0];
                found = true;
            }
        }
        return found ? Math.round(total) : null;
    }

    function relaxToCall(compactTable, heroSeatId) {
        if (!Array.isArray(compactTable) || typeof heroSeatId !== 'number') {
            return null;
        }
        var bets = Array.isArray(compactTable[3]) ? compactTable[3] : null;
        if (!bets || heroSeatId < 0 || heroSeatId >= bets.length || typeof bets[heroSeatId] !== 'number') {
            return null;
        }
        var highestBet = 0;
        for (var i = 0; i < bets.length; i += 1) {
            if (typeof bets[i] === 'number') {
                highestBet = Math.max(highestBet, bets[i]);
            }
        }
        return Math.max(0, Math.round(highestBet - bets[heroSeatId]));
    }

    function relaxMinimumRaise(compactAction) {
        if (!Array.isArray(compactAction) || !Array.isArray(compactAction[3])) {
            return null;
        }
        var options = compactAction[3];
        for (var i = 0; i < options.length; i += 1) {
            if (Array.isArray(options[i]) && options[i][0] === 3 && typeof options[i][1] === 'number') {
                return Math.max(0, Math.round(options[i][1]));
            }
        }
        return 0;
    }

    function relaxPlayerNames(compactTable) {
        if (!Array.isArray(compactTable) || typeof compactTable[0] !== 'string') {
            return null;
        }
        return compactTable[0].split('|').map(function (name) {
            return String(name).trim().toLowerCase();
        });
    }

    function relaxHeroSeatFromNames(compactTable) {
        var heroName = DEFAULT_RELAX_HERO_NAME.trim().toLowerCase();
        if (!heroName) {
            return null;
        }
        var names = relaxPlayerNames(compactTable);
        if (!names) {
            return null;
        }
        for (var i = 0; i < names.length; i += 1) {
            if (names[i] === heroName) {
                return i;
            }
        }
        return null;
    }

    function relaxAcceptsPlayerContext(compactTable, playerSeatId) {
        if (typeof playerSeatId !== 'number') {
            return false;
        }
        var heroSeatId = relaxHeroSeatFromNames(compactTable);
        if (heroSeatId !== null) {
            return playerSeatId === heroSeatId;
        }
        if (relaxPlayerNames(compactTable)) {
            return false;
        }
        return typeof relaxHandState.heroSeatId === 'number' && playerSeatId === relaxHandState.heroSeatId;
    }

    function resetRelaxHandState(handId) {
        relaxHandState.handId = handId;
        relaxHandState.heroSeatId = null;
        relaxHandState.holeKey = null;
        relaxHandState.holeCards = null;
        relaxHandState.boardKey = null;
        relaxHandState.boardCards = [];
        relaxHandState.playersCount = null;
        relaxHandState.heroSittingOut = null;
        relaxHandState.heroFolded = null;
        relaxHandState.pot = null;
        relaxHandState.toCall = null;
        relaxHandState.minimumRaise = null;
        relaxHandState.heroTurn = null;
        relaxHandState.resetSent = false;

        handState.handId = handId;
        handState.activeSeats = {};
        handState.activeCount = null;
        handState.boardCount = 0;
        handState.holeSentForHand = false;
        handState.heroSeatId = null;
        handState.heroSittingOut = null;
    }

    function payloadFromRelaxBody(messageBody) {
        if (!messageBody || !Array.isArray(messageBody.tags)) {
            return null;
        }
        var compactPayload = messageBody.payLoad;
        if (!compactPayload || typeof compactPayload !== 'object' || typeof compactPayload.hid !== 'number') {
            return null;
        }

        var tags = messageBody.tags;
        var isInit = tags.indexOf('init') !== -1;
        var isNewHand = relaxHandState.handId !== compactPayload.hid;
        if (isNewHand) {
            resetRelaxHandState(compactPayload.hid);
        }

        var payload = {
            type: 'poker_cards',
            handId: compactPayload.hid
        };
        if (typeof compactPayload.tid === 'number') {
            payload.tableId = compactPayload.tid;
        }
        var changed = false;
        var holeChanged = false;
        if ((isNewHand || isInit) && !relaxHandState.resetSent) {
            payload.reset = true;
            payload.board = [];
            relaxHandState.resetSent = true;
            relaxHandState.boardKey = '';
            changed = true;
        }

        var playerContext = Array.isArray(compactPayload.p) ? compactPayload.p : null;
        var compactTable = Array.isArray(compactPayload.c) ? compactPayload.c : null;
        var playerSeatId = playerContext && typeof playerContext[1] === 'number' ? playerContext[1] : null;
        var namedHeroSeatId = relaxHeroSeatFromNames(compactTable);
        var heroSeatId = namedHeroSeatId !== null ? namedHeroSeatId : relaxHandState.heroSeatId;
        if (heroSeatId === null && relaxAcceptsPlayerContext(compactTable, playerSeatId)) {
            heroSeatId = playerSeatId;
        }
        if (heroSeatId !== null) {
            relaxHandState.heroSeatId = heroSeatId;
            handState.heroSeatId = heroSeatId;
            payload.heroSeatId = heroSeatId;
        }

        var states = compactTable && Array.isArray(compactTable[1]) ? compactTable[1] : null;
        var playersCount = relaxActivePlayers(states);
        if (playersCount !== null && playersCount !== relaxHandState.playersCount) {
            relaxHandState.playersCount = playersCount;
            handState.activeCount = playersCount;
            payload.players = playersCount;
            changed = true;
        }

        if (states && heroSeatId !== null && heroSeatId >= 0 && heroSeatId < states.length) {
            var heroState = states[heroSeatId];
            if (heroState === 6 && relaxHandState.heroSittingOut !== true) {
                relaxHandState.heroSittingOut = true;
                handState.heroSittingOut = true;
                payload.heroSittingOut = true;
                changed = true;
            } else if ((isInit || tags.indexOf('deal') !== -1) && heroState === 1 && relaxHandState.heroSittingOut !== false) {
                relaxHandState.heroSittingOut = false;
                handState.heroSittingOut = false;
                payload.heroSittingOut = false;
                changed = true;
            }
            var heroFolded = heroState === 3 || heroState === 4;
            if (heroFolded !== relaxHandState.heroFolded) {
                relaxHandState.heroFolded = heroFolded;
                payload.heroFolded = heroFolded;
                changed = true;
            }
        }

        var compactAction = Array.isArray(compactPayload.d) ? compactPayload.d : null;
        var heroTurn = relaxHandState.heroTurn;
        if (isNewHand) {
            heroTurn = false;
        }
        if (tags.indexOf('pturn') !== -1 && compactAction && typeof compactAction[0] === 'number') {
            heroTurn = compactAction[0] === heroSeatId;
        } else if (
            (tags.indexOf('flop') !== -1 || tags.indexOf('turn') !== -1 || tags.indexOf('river') !== -1) ||
            (tags.indexOf('act') !== -1 && compactAction && compactAction[0] === heroSeatId) ||
            tags.indexOf('finished') !== -1
        ) {
            heroTurn = false;
        }
        if (heroTurn !== null && heroTurn !== relaxHandState.heroTurn) {
            relaxHandState.heroTurn = heroTurn;
            payload.heroTurn = heroTurn;
            changed = true;
        }

        var pot = relaxPotSize(compactTable);
        if (pot !== null && pot !== relaxHandState.pot) {
            relaxHandState.pot = pot;
            payload.pot = pot;
            changed = true;
        }

        var toCall = relaxToCall(compactTable, heroSeatId);
        if (toCall !== null && toCall !== relaxHandState.toCall) {
            relaxHandState.toCall = toCall;
            payload.toCall = toCall;
            changed = true;
        }

        if (heroTurn === true && tags.indexOf('pturn') !== -1) {
            var minimumRaise = relaxMinimumRaise(compactAction);
            if (minimumRaise !== null && minimumRaise !== relaxHandState.minimumRaise) {
                relaxHandState.minimumRaise = minimumRaise;
                payload.minimumRaise = minimumRaise;
                changed = true;
            }
        }

        var holeCards = playerContext ? normalizeCompactCards(playerContext[3], 2) : null;
        var isReliableHoleFrame = tags.indexOf('deal') !== -1 || tags.indexOf('pturn') !== -1;
        if (holeCards && isReliableHoleFrame && relaxAcceptsPlayerContext(compactTable, playerSeatId)) {
            var holeKey = holeCards.join('');
            if (holeKey !== relaxHandState.holeKey) {
                relaxHandState.holeKey = holeKey;
                handState.holeSentForHand = true;
                holeChanged = true;
                changed = true;
            }
            relaxHandState.holeCards = holeCards.slice();
        }

        var compactBoard = compactTable && typeof compactTable[7] === 'string' ? compactTable[7] : null;
        var boardCount = compactBoard ? compactBoard.length / 2 : 0;
        var boardCards = boardCount >= 3 && boardCount <= 5
            ? normalizeCompactCards(compactBoard, boardCount)
            : null;
        if (boardCards) {
            var boardKey = boardCards.join('');
            if (boardKey !== relaxHandState.boardKey) {
                relaxHandState.boardKey = boardKey;
                handState.boardCount = boardCards.length;
                changed = true;
            }
            relaxHandState.boardCards = boardCards.slice();
        }

        if (!changed) {
            return null;
        }
        // Card context is deliberately repeated. If the desktop app restarts or
        // misses a POST, the next state change can rebuild the complete hand.
        if (relaxHandState.holeCards && holeChanged) {
            payload.hole = relaxHandState.holeCards.slice();
        }
        if (!Object.prototype.hasOwnProperty.call(payload, 'board')) {
            payload.board = relaxHandState.boardCards.slice();
        }
        if (relaxHandState.heroSeatId !== null && !Object.prototype.hasOwnProperty.call(payload, 'heroSeatId')) {
            payload.heroSeatId = relaxHandState.heroSeatId;
        }
        if (relaxHandState.heroSittingOut !== null && !Object.prototype.hasOwnProperty.call(payload, 'heroSittingOut')) {
            payload.heroSittingOut = relaxHandState.heroSittingOut;
        }
        if (relaxHandState.heroFolded !== null && !Object.prototype.hasOwnProperty.call(payload, 'heroFolded')) {
            payload.heroFolded = relaxHandState.heroFolded;
        }
        if (relaxHandState.heroTurn !== null && !Object.prototype.hasOwnProperty.call(payload, 'heroTurn')) {
            payload.heroTurn = relaxHandState.heroTurn;
        }
        return payload;
    }

    function processRelaxPokerFrame(frameData) {
        var bodies = parseRelaxFrameBodies(frameData);
        for (var i = 0; i < bodies.length; i += 1) {
            var payload = payloadFromRelaxBody(bodies[i]);
            if (!payload) {
                continue;
            }
            if (!relaxDecoderReported) {
                relaxDecoderReported = true;
                sendDebug('Relax poker decoder active');
            }
            sendPayload(payload);
        }
    }

    function isPlayerActive(player) {
        if (!player || typeof player !== 'object') {
            return false;
        }
        if (player.folded === true || player.isFolded === true) {
            return false;
        }
        if (player.inHand === false) {
            return false;
        }
        if (typeof player.state === 'string') {
            var state = player.state.toLowerCase();
            if (state === 'fold' || state === 'folded' || state === 'out' || state === 'sittingout') {
                return false;
            }
        }
        return true;
    }

    function isPlayerSittingOut(player) {
        if (!player || typeof player !== 'object') {
            return false;
        }
        if (player.sitOut === true || player.sittingOut === true || player.isSittingOut === true) {
            return true;
        }
        if (typeof player.state === 'string') {
            var state = player.state.toLowerCase();
            if (state === 'sitout' || state === 'sittingout' || state === 'out') {
                return true;
            }
        }
        return false;
    }

    function getSeatId(player) {
        if (!player || typeof player !== 'object') {
            return null;
        }
        if (typeof player.seatId === 'number') {
            return player.seatId;
        }
        if (typeof player.seat === 'number') {
            return player.seat;
        }
        if (player.seat && typeof player.seat.id === 'number') {
            return player.seat.id;
        }
        return null;
    }

    function isTrustedPlayersAction(action) {
        return action === 'startHand' || action === 'dealHoleCards' || action === 'blinds' || action === 'call' || action === 'bet' || action === 'raise' || action === 'allIn' || action === 'check' || action === 'finishHand' || action === 'resetTable';
    }

    function normalizeUserId(value) {
        if (typeof value === 'number' && isFinite(value)) {
            return String(value);
        }
        if (typeof value === 'string') {
            var trimmed = value.trim();
            return trimmed ? trimmed : null;
        }
        return null;
    }

    function extractUserId(value) {
        if (!value || typeof value !== 'object') {
            return null;
        }
        var direct = normalizeUserId(value.userId);
        if (direct) {
            return direct;
        }
        if (value.user && typeof value.user === 'object') {
            var nested = normalizeUserId(value.user.id);
            if (nested) {
                return nested;
            }
        }
        return null;
    }

    function isLikelyFullSnapshot(players, action) {
        if (!Array.isArray(players) || players.length < 2) {
            return false;
        }
        if (isTrustedPlayersAction(action)) {
            return true;
        }
        if (handState.activeCount === null) {
            return players.length >= 3;
        }
        return players.length >= Math.max(3, handState.activeCount - 1);
    }

    function rebuildActiveSeatsFromPlayers(players, handId, action) {
        if (!isLikelyFullSnapshot(players, action)) {
            return null;
        }
        if (typeof handId === 'number' && handState.handId !== handId) {
            handState.handId = handId;
            handState.activeSeats = {};
            handState.activeCount = null;
            handState.boardCount = 0;
            handState.holeSentForHand = false;
        }

        var next = {};
        for (var i = 0; i < players.length; i += 1) {
            var player = players[i];
            var seatId = getSeatId(player);
            if (seatId === null) {
                continue;
            }
            next[seatId] = isPlayerActive(player);
        }

        var active = 0;
        var keys = Object.keys(next);
        for (var k = 0; k < keys.length; k += 1) {
            if (next[keys[k]]) {
                active += 1;
            }
        }

        if (active <= 0 && keys.length > 0) {
            active = keys.length;
        }
        if (active <= 0) {
            return null;
        }

        handState.activeSeats = next;
        handState.activeCount = Math.max(2, Math.min(10, active));
        return handState.activeCount;
    }

    function markSeatFolded(seatId, handId) {
        if (typeof seatId !== 'number') {
            return null;
        }
        if (typeof handId === 'number' && handState.handId !== handId) {
            handState.handId = handId;
            handState.activeSeats = {};
            handState.activeCount = null;
            handState.boardCount = 0;
            handState.holeSentForHand = false;
        }

        if (!Object.prototype.hasOwnProperty.call(handState.activeSeats, seatId)) {
            return null;
        }

        if (handState.activeSeats[seatId] === false) {
            return handState.activeCount;
        }

        handState.activeSeats[seatId] = false;

        var keys = Object.keys(handState.activeSeats);
        if (keys.length === 0) {
            return null;
        }
        var active = 0;
        for (var i = 0; i < keys.length; i += 1) {
            if (handState.activeSeats[keys[i]]) {
                active += 1;
            }
        }
        if (active <= 0) {
            active = 2;
        }
        handState.activeCount = Math.max(2, Math.min(10, active));
        return handState.activeCount;
    }

    function extractPlayersCount(players) {
        if (!Array.isArray(players) || players.length < 2) {
            return null;
        }
        var active = 0;
        for (var i = 0; i < players.length; i += 1) {
            if (isPlayerActive(players[i])) {
                active += 1;
            }
        }
        if (active <= 0) {
            active = players.length;
        }
        if (active <= 0) {
            return null;
        }
        return Math.max(2, Math.min(10, active));
    }

    function attachContext(payload, handId) {
        if (!payload || typeof payload !== 'object') {
            return payload;
        }
        var contextHandId = typeof handId === 'number' ? handId : handState.handId;
        if (typeof contextHandId === 'number') {
            payload.handId = contextHandId;
        }
        if (handState.heroUserId) {
            payload.heroUserId = handState.heroUserId;
        }
        if (typeof handState.heroSeatId === 'number') {
            payload.heroSeatId = handState.heroSeatId;
        }
        if (typeof handState.heroSittingOut === 'boolean') {
            payload.heroSittingOut = handState.heroSittingOut;
        }
        return payload;
    }

    function refreshHeroContext(players) {
        if (!Array.isArray(players) || !handState.heroUserId) {
            return;
        }
        for (var i = 0; i < players.length; i += 1) {
            var player = players[i];
            if (!player || typeof player !== 'object') {
                continue;
            }
            if (extractUserId(player) !== handState.heroUserId) {
                continue;
            }
            var seatId = getSeatId(player);
            if (seatId !== null) {
                handState.heroSeatId = seatId;
            }
            handState.heroSittingOut = isPlayerSittingOut(player);
            return;
        }
    }

    function maybeHolePayloadFromPlayerObject(value, playersCount) {
        if (!value || typeof value !== 'object') {
            return null;
        }
        if (handState.holeSentForHand || handState.boardCount > 0) {
            return null;
        }
        if (!Object.prototype.hasOwnProperty.call(value, 'cards')) {
            return null;
        }
        if (!Object.prototype.hasOwnProperty.call(value, 'seatId') && !Object.prototype.hasOwnProperty.call(value, 'userId')) {
            return null;
        }

        var playerUserId = extractUserId(value);
        var playerSeatId = getSeatId(value);
        var matchesHero = false;
        if (handState.heroUserId && playerUserId && playerUserId === handState.heroUserId) {
            matchesHero = true;
        }
        if (!matchesHero && typeof handState.heroSeatId === 'number' && typeof playerSeatId === 'number' && playerSeatId === handState.heroSeatId) {
            matchesHero = true;
        }
        if (!matchesHero) {
            return null;
        }

        if (typeof playerSeatId === 'number') {
            handState.heroSeatId = playerSeatId;
        }
        handState.heroSittingOut = isPlayerSittingOut(value);

        var cards = extractCardsFromValue(value.cards, 2);
        if (!cards || cards.length !== 2) {
            return null;
        }

        handState.holeSentForHand = true;
        var payload = { type: 'poker_cards', hole: cards };
        if (playersCount !== null) {
            payload.players = playersCount;
        }
        return attachContext(payload, handState.handId);
    }

    function extractCardsFromValue(value, maxCount) {
        if (Array.isArray(value)) {
            var normalizedArray = normalizeCardArray(value, maxCount);
            if (normalizedArray && normalizedArray.length > 0) {
                return normalizedArray;
            }
            return null;
        }

        if (typeof value === 'string') {
            var matches = value.match(/\b[2-9TJQKA][cdhsCDHS]\b/g);
            if (!matches) {
                return null;
            }
            var extracted = [];
            for (var i = 0; i < matches.length && extracted.length < maxCount; i += 1) {
                var normalized = normalizeCard(matches[i]);
                if (normalized) {
                    extracted.push(normalized);
                }
            }
            return extracted.length > 0 ? extracted : null;
        }

        return null;
    }

    function walkForPayload(value, seen, kindHint) {
        if (!value || typeof value !== 'object') {
            return null;
        }
        if (seen.indexOf(value) !== -1) {
            return null;
        }

        if (!handState.heroUserId) {
            var candidateUserId = extractUserId(value);
            if (candidateUserId && (Object.prototype.hasOwnProperty.call(value, 'token') || Object.prototype.hasOwnProperty.call(value, 'tableId'))) {
                handState.heroUserId = candidateUserId;
            }
        }
        seen.push(value);

        if (Array.isArray(value)) {
            for (var i = 0; i < value.length; i += 1) {
                var child = walkForPayload(value[i], seen, kindHint);
                if (child) {
                    return child;
                }
            }
            return null;
        }

        if (Object.prototype.hasOwnProperty.call(value, 'action')) {
            var action = String(value.action || '');
            var handId = typeof value.handId === 'number' ? value.handId : null;
            if (handId !== null) {
                handState.handId = handId;
            }
            var playersCount = rebuildActiveSeatsFromPlayers(value.players, handId, action);
            if (playersCount === null && isTrustedPlayersAction(action)) {
                playersCount = extractPlayersCount(value.players);
            }
            refreshHeroContext(value.players);

            if (action === 'startHand') {
                if (handId !== null) {
                    handState.handId = handId;
                    handState.activeSeats = {};
                    handState.activeCount = null;
                }
                handState.boardCount = 0;
                handState.holeSentForHand = false;
                var startPayload = { type: 'poker_cards', reset: true, board: [] };
                if (playersCount !== null) {
                    startPayload.players = playersCount;
                }
                return attachContext(startPayload, handId);
            }

            if (action === 'authenticated') {
                var authenticatedUserId = extractUserId(value);
                if (authenticatedUserId) {
                    handState.heroUserId = authenticatedUserId;
                    handState.heroSeatId = null;
                    handState.heroSittingOut = null;
                }
            }

            if (action === 'resetTable' || action === 'finishHand') {
                handState.boardCount = 0;
                handState.holeSentForHand = false;
            }

            if (action === 'fold') {
                var foldedCount = markSeatFolded(value.seatId, handId);
                if (foldedCount !== null) {
                    return attachContext({ type: 'poker_cards', players: foldedCount }, handId);
                }
            }

            if (action === 'dealCommunityCards') {
                var boardCards = extractCardsFromValue(value.cards, 5);
                if (boardCards && boardCards.length >= 1) {
                    handState.boardCount = Math.min(5, handState.boardCount + boardCards.length);
                    var boardPayload = { type: 'poker_cards', board: boardCards.slice(0, 5) };
                    if (playersCount !== null) {
                        boardPayload.players = playersCount;
                    }
                    return attachContext(boardPayload, handId);
                }
            }
            if (action === 'dealHoleCards') {
                var holeCards = extractCardsFromValue(value.cards, 2);
                if (holeCards && holeCards.length === 2) {
                    handState.holeSentForHand = true;
                    var holePayload = { type: 'poker_cards', hole: holeCards };
                    if (playersCount !== null) {
                        holePayload.players = playersCount;
                    }
                    return attachContext(holePayload, handId);
                }
                if (Array.isArray(value.players)) {
                    var candidates = [];
                    for (var p = 0; p < value.players.length; p += 1) {
                        var player = value.players[p];
                        if (!player || typeof player !== 'object' || !Array.isArray(player.cards)) {
                            continue;
                        }
                        var playerCards = extractCardsFromValue(player.cards, 2);
                        if (!playerCards || playerCards.length !== 2) {
                            continue;
                        }
                        candidates.push({
                            cards: playerCards,
                            userId: extractUserId(player),
                            seatId: getSeatId(player),
                            sittingOut: isPlayerSittingOut(player)
                        });
                    }

                    var chosen = null;
                    if (handState.heroUserId) {
                        for (var hp = 0; hp < candidates.length; hp += 1) {
                            if (candidates[hp].userId && candidates[hp].userId === handState.heroUserId) {
                                chosen = candidates[hp];
                                break;
                            }
                        }
                    }
                    if (!chosen && typeof handState.heroSeatId === 'number') {
                        for (var hs = 0; hs < candidates.length; hs += 1) {
                            if (typeof candidates[hs].seatId === 'number' && candidates[hs].seatId === handState.heroSeatId) {
                                chosen = candidates[hs];
                                break;
                            }
                        }
                    }
                    if (!chosen && candidates.length === 1) {
                        chosen = candidates[0];
                    }

                    if (chosen) {
                        handState.holeSentForHand = true;
                        if (chosen.userId) {
                            handState.heroUserId = chosen.userId;
                        }
                        if (typeof chosen.seatId === 'number') {
                            handState.heroSeatId = chosen.seatId;
                        }
                        handState.heroSittingOut = chosen.sittingOut;

                        var chosenHolePayload = { type: 'poker_cards', hole: chosen.cards };
                        if (playersCount !== null) {
                            chosenHolePayload.players = playersCount;
                        }
                        return attachContext(chosenHolePayload, handId);
                    }
                }
            }
                if (action === 'show' || action === 'showdown' || action === 'awardPot' || action === 'finishHand') {
                    if (playersCount !== null) {
                        return attachContext({ type: 'poker_cards', players: playersCount }, handId);
                    }
                    return null;
                }

            if (playersCount !== null) {
                return attachContext({ type: 'poker_cards', players: playersCount }, handId);
            }
        }

        var playerHolePayloadFromObject = maybeHolePayloadFromPlayerObject(value, handState.activeCount);
        if (playerHolePayloadFromObject) {
            return playerHolePayloadFromObject;
        }

        var keys = Object.keys(value);
        for (var k = 0; k < keys.length; k += 1) {
            var nested = walkForPayload(value[keys[k]], seen, kindHint);
            if (nested) {
                return nested;
            }
        }

        return null;
    }

    function payloadFromArgs(args) {
        for (var i = 0; i < args.length; i += 1) {
            var arg = args[i];
            if (arg && typeof arg === 'object') {
                var objectPayload = walkForPayload(arg, [], null);
                if (objectPayload) {
                    return objectPayload;
                }
            }
        }
        return null;
    }

    function taggedLineFromArgs(args) {
        for (var i = 0; i < args.length; i += 1) {
            if (typeof args[i] === 'string') {
                var line = args[i].trim();
                if (looksTaggedLine(line)) {
                    return line;
                }
            }
        }
        return null;
    }

    function sendPokerCards(payload) {
        var safePayload = {
            type: 'poker_cards'
        };

        if (Object.prototype.hasOwnProperty.call(payload, 'hole')) {
            var hole = normalizeCardArray(payload.hole, 2);
            if (!hole || hole.length !== 2) {
                console.warn('PokerOdds bridge: hole must be exactly 2 cards');
                return;
            }
            safePayload.hole = hole;
        }

        if (Object.prototype.hasOwnProperty.call(payload, 'board')) {
            var board = normalizeCardArray(payload.board, 5);
            if (!board) {
                console.warn('PokerOdds bridge: board must be up to 5 cards');
                return;
            }
            safePayload.board = board;
        }

        if (!Object.prototype.hasOwnProperty.call(safePayload, 'hole') && !Object.prototype.hasOwnProperty.call(safePayload, 'board')) {
            console.warn('PokerOdds bridge: provide hole and/or board');
            return;
        }

        sendLine(BRIDGE_TAG + JSON.stringify(safePayload));
    }

    function hookConsole(consoleObj, sourceLabel) {
        if (!consoleObj || consoleObj[HOOK_FLAG]) {
            return;
        }
        consoleObj[HOOK_FLAG] = true;

        var methods = ['log', 'info', 'warn', 'error', 'debug'];
        methods.forEach(function (method) {
            var original = consoleObj[method];
            if (typeof original !== 'function') {
                return;
            }
            consoleObj[method] = function () {
                var args = Array.prototype.slice.call(arguments);
                try {
                    original.apply(consoleObj, args);
                } catch (_) {}

                sendRawLine(sourceLabel, method.toUpperCase(), args);
                sendStrategyEventLines(sourceLabel, args);

                var tagged = taggedLineFromArgs(args);
                if (tagged) {
                    sendLine(tagged);
                }

                var payload = payloadFromArgs(args);
                if (payload) {
                    sendPayload(payload);
                }
            };
        });

        sendDebug('hooked console source=' + sourceLabel);
    }

    function installGlobalErrorCapture(target, sourceLabel) {
        if (!target || target.__tmPokerBridgeErrorHooked__) {
            return;
        }
        target.__tmPokerBridgeErrorHooked__ = true;

        try {
            target.addEventListener('error', function (event) {
                var msg = event && event.message ? event.message : 'unknown error';
                var file = event && event.filename ? event.filename : '';
                var line = event && typeof event.lineno === 'number' ? event.lineno : 0;
                var col = event && typeof event.colno === 'number' ? event.colno : 0;
                sendRawLine(sourceLabel, 'UNCAUGHT', [msg, file + ':' + line + ':' + col]);
            }, true);
        } catch (_) {}

        try {
            target.addEventListener('unhandledrejection', function (event) {
                var reason = event ? event.reason : null;
                sendRawLine(sourceLabel, 'UNHANDLEDREJECTION', [reason]);
            }, true);
        } catch (_) {}
    }

    function installIframeDiscovery(target, sourceLabel) {
        if (!target || !shouldDiscoverIframes() || target.__tmPokerBridgeIframeHooked__) {
            return;
        }
        target.__tmPokerBridgeIframeHooked__ = true;

        function logIframe(iframe, reason) {
            if (!iframe || !iframe.tagName || String(iframe.tagName).toLowerCase() !== 'iframe') {
                return;
            }
            var src = iframe.getAttribute('src') || iframe.src || '(no src)';
            sendDiscoveryLine(sourceLabel, 'IFRAME', [reason, src]);
        }

        try {
            var existing = target.document ? target.document.getElementsByTagName('iframe') : [];
            for (var i = 0; i < existing.length; i += 1) {
                logIframe(existing[i], 'existing');
            }
        } catch (_) {}

        try {
            if (!target.MutationObserver || !target.document || !target.document.documentElement) {
                return;
            }
            var observer = new target.MutationObserver(function (mutations) {
                for (var m = 0; m < mutations.length; m += 1) {
                    if (mutations[m].type === 'attributes') {
                        logIframe(mutations[m].target, 'src-changed');
                        continue;
                    }
                    var nodes = mutations[m].addedNodes || [];
                    for (var n = 0; n < nodes.length; n += 1) {
                        var node = nodes[n];
                        logIframe(node, 'added');
                        if (node && node.querySelectorAll) {
                            var nested = node.querySelectorAll('iframe');
                            for (var j = 0; j < nested.length; j += 1) {
                                logIframe(nested[j], 'nested');
                            }
                        }
                    }
                }
            });
            observer.observe(target.document.documentElement, {
                attributes: true,
                attributeFilter: ['src'],
                childList: true,
                subtree: true
            });
            sendDebug('hooked iframe discovery source=' + sourceLabel);
        } catch (_) {}
    }

    function installWebSocketDiscovery(target, sourceLabel) {
        if (!target || !isDiscoveryHost() || target.__tmPokerBridgeWebSocketHooked__) {
            return;
        }
        var OriginalWebSocket = target.WebSocket;
        if (typeof OriginalWebSocket !== 'function') {
            return;
        }
        target.__tmPokerBridgeWebSocketHooked__ = true;
        var nextSocketId = 1;
        var fallbackSocketId = 1;
        var seenMessageEvents = typeof WeakSet === 'function' ? new WeakSet() : null;

        function socketDetails(socket) {
            var socketId = socket.__tmPokerBridgeSocketId;
            if (!socketId) {
                socketId = 'existing-' + fallbackSocketId;
                fallbackSocketId += 1;
                try {
                    socket.__tmPokerBridgeSocketId = socketId;
                    socket.__tmPokerBridgeSocketUrl = socket.url || '[unknown-ws-url]';
                } catch (_) {}
            }
            return {
                id: socketId,
                url: socket.__tmPokerBridgeSocketUrl || socket.url || '[unknown-ws-url]'
            };
        }

        function logIncomingMessage(socket, event, captureSource) {
            if (event && seenMessageEvents && seenMessageEvents.has(event)) {
                return;
            }
            if (event && seenMessageEvents) {
                seenMessageEvents.add(event);
            }
            var details = socketDetails(socket);
            var data = event ? event.data : null;
            processRelaxPokerFrame(data);
            sendDiscoveryLine(sourceLabel, 'WS_MESSAGE', [
                '#' + details.id,
                details.url,
                captureSource,
                summarizeWebSocketBody(data)
            ]);
            if (typeof Blob !== 'undefined' && data instanceof Blob && typeof data.arrayBuffer === 'function') {
                data.arrayBuffer().then(function (buffer) {
                    sendDiscoveryLine(sourceLabel, 'WS_MESSAGE_BINARY', [
                        '#' + details.id,
                        details.url,
                        binaryPreview('Blob', new Uint8Array(buffer))
                    ]);
                }).catch(function () {});
            }
        }

        var webSocketPrototype = OriginalWebSocket.prototype;
        var originalAddEventListener = webSocketPrototype && webSocketPrototype.addEventListener;
        var originalRemoveEventListener = webSocketPrototype && webSocketPrototype.removeEventListener;
        var wrappedMessageListeners = typeof WeakMap === 'function' ? new WeakMap() : null;

        function listenerMapFor(socket) {
            if (!wrappedMessageListeners) {
                return null;
            }
            var listenerMap = wrappedMessageListeners.get(socket);
            if (!listenerMap) {
                listenerMap = new WeakMap();
                wrappedMessageListeners.set(socket, listenerMap);
            }
            return listenerMap;
        }

        function attachSocketLogging(socket, socketId, url) {
            try {
                originalAddEventListener.call(socket, 'open', function () {
                    sendDiscoveryLine(sourceLabel, 'WS_OPEN', ['#' + socketId, url]);
                });
                originalAddEventListener.call(socket, 'message', function (event) {
                    logIncomingMessage(socket, event, 'socket-listener');
                });
                originalAddEventListener.call(socket, 'close', function (event) {
                    var code = event && typeof event.code === 'number' ? event.code : '-';
                    var reason = event && typeof event.reason === 'string' ? event.reason : '';
                    sendDiscoveryLine(sourceLabel, 'WS_CLOSE', ['#' + socketId, url, code, reason]);
                });
                originalAddEventListener.call(socket, 'error', function () {
                    sendDiscoveryLine(sourceLabel, 'WS_ERROR', ['#' + socketId, url]);
                });
            } catch (_) {}
        }

        if (typeof originalAddEventListener === 'function' && !webSocketPrototype.__tmPokerBridgeListenerHooked__) {
            webSocketPrototype.__tmPokerBridgeListenerHooked__ = true;
            webSocketPrototype.addEventListener = function (type, listener) {
                if (type === 'message' && listener) {
                    var socket = this;
                    var listenerMap = listenerMapFor(socket);
                    var wrappedListener = listenerMap && listenerMap.get(listener);
                    if (!wrappedListener) {
                        wrappedListener = typeof listener === 'function'
                            ? function (event) {
                                logIncomingMessage(socket, event, 'addEventListener');
                                return listener.apply(this, arguments);
                            }
                            : {
                                handleEvent: function (event) {
                                    logIncomingMessage(socket, event, 'addEventListener');
                                    return listener.handleEvent.apply(listener, arguments);
                                }
                            };
                        if (listenerMap) {
                            listenerMap.set(listener, wrappedListener);
                        }
                    }
                    return originalAddEventListener.call(this, type, wrappedListener, arguments[2]);
                }
                return originalAddEventListener.apply(this, arguments);
            };
            if (typeof originalRemoveEventListener === 'function') {
                webSocketPrototype.removeEventListener = function (type, listener) {
                    if (type === 'message' && listener && wrappedMessageListeners) {
                        var listenerMap = wrappedMessageListeners.get(this);
                        if (listenerMap && listenerMap.has(listener)) {
                            var wrappedListener = listenerMap.get(listener);
                            listenerMap.delete(listener);
                            return originalRemoveEventListener.call(this, type, wrappedListener, arguments[2]);
                        }
                    }
                    return originalRemoveEventListener.apply(this, arguments);
                };
            }
        }

        try {
            var onMessageDescriptor = Object.getOwnPropertyDescriptor(webSocketPrototype, 'onmessage');
            if (
                onMessageDescriptor &&
                typeof onMessageDescriptor.get === 'function' &&
                typeof onMessageDescriptor.set === 'function' &&
                !webSocketPrototype.__tmPokerBridgeOnMessageHooked__
            ) {
                webSocketPrototype.__tmPokerBridgeOnMessageHooked__ = true;
                var assignedMessageHandlers = typeof WeakMap === 'function' ? new WeakMap() : null;
                Object.defineProperty(webSocketPrototype, 'onmessage', {
                    configurable: onMessageDescriptor.configurable,
                    enumerable: onMessageDescriptor.enumerable,
                    get: function () {
                        if (assignedMessageHandlers && assignedMessageHandlers.has(this)) {
                            return assignedMessageHandlers.get(this);
                        }
                        return onMessageDescriptor.get.call(this);
                    },
                    set: function (listener) {
                        if (assignedMessageHandlers) {
                            assignedMessageHandlers.set(this, listener);
                        }
                        if (typeof listener !== 'function') {
                            return onMessageDescriptor.set.call(this, listener);
                        }
                        var socket = this;
                        return onMessageDescriptor.set.call(this, function (event) {
                            logIncomingMessage(socket, event, 'onmessage');
                            return listener.apply(this, arguments);
                        });
                    }
                });
            }
        } catch (_) {}

        var originalSend = OriginalWebSocket.prototype && OriginalWebSocket.prototype.send;
        if (typeof originalSend === 'function' && !OriginalWebSocket.prototype.__tmPokerBridgeSendHooked__) {
            OriginalWebSocket.prototype.__tmPokerBridgeSendHooked__ = true;
            OriginalWebSocket.prototype.send = function (data) {
                var socketId = this.__tmPokerBridgeSocketId || '?';
                var url = this.__tmPokerBridgeSocketUrl || this.url || '[unknown-ws-url]';
                sendDiscoveryLine(sourceLabel, 'WS_SEND', ['#' + socketId, url, summarizeWebSocketBody(data)]);
                return originalSend.apply(this, arguments);
            };
        }

        function WrappedWebSocket(url, protocols) {
            var socket = protocols !== undefined ? new OriginalWebSocket(url, protocols) : new OriginalWebSocket(url);
            var socketId = nextSocketId;
            nextSocketId += 1;
            try {
                socket.__tmPokerBridgeSocketId = socketId;
                socket.__tmPokerBridgeSocketUrl = String(url);
            } catch (_) {}
            sendDiscoveryLine(sourceLabel, 'WS_CREATE', ['#' + socketId, String(url)]);
            attachSocketLogging(socket, socketId, String(url));
            return socket;
        }

        WrappedWebSocket.prototype = OriginalWebSocket.prototype;
        try {
            Object.setPrototypeOf(WrappedWebSocket, OriginalWebSocket);
        } catch (_) {}
        ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (key) {
            try {
                WrappedWebSocket[key] = OriginalWebSocket[key];
            } catch (_) {}
        });
        target.WebSocket = WrappedWebSocket;
        sendDebug('hooked websocket discovery source=' + sourceLabel);
    }

    function installFetchDiscovery(target, sourceLabel) {
        if (!target || !isDiscoveryHost() || target.__tmPokerBridgeFetchHooked__) {
            return;
        }
        var originalFetch = target.fetch;
        if (typeof originalFetch !== 'function') {
            return;
        }
        target.__tmPokerBridgeFetchHooked__ = true;
        target.fetch = function (input, init) {
            var url = requestUrl(input);
            var method = requestMethod(input, init);
            var body = init && Object.prototype.hasOwnProperty.call(init, 'body') ? summarizeBody(init.body) : '-';
            sendDiscoveryLine(sourceLabel, 'FETCH_REQUEST', [method, url, body]);
            var result = originalFetch.apply(this, arguments);
            try {
                result.then(function (response) {
                    var status = response && typeof response.status === 'number' ? response.status : '-';
                    var contentType = response && response.headers && response.headers.get ? (response.headers.get('content-type') || '-') : '-';
                    sendDiscoveryLine(sourceLabel, 'FETCH_RESPONSE', [method, url, status, contentType]);
                    if (response && response.clone && /json|text|javascript|xml/i.test(contentType)) {
                        response.clone().text().then(function (text) {
                            sendDiscoveryLine(sourceLabel, 'FETCH_BODY', [method, url, truncateDiscoveryText(text)]);
                        }).catch(function () {});
                    }
                }).catch(function (error) {
                    sendDiscoveryLine(sourceLabel, 'FETCH_ERROR', [method, url, summarizeBody(error)]);
                });
            } catch (_) {}
            return result;
        };
        sendDebug('hooked fetch discovery source=' + sourceLabel);
    }

    function installXhrDiscovery(target, sourceLabel) {
        if (!target || !isDiscoveryHost() || target.__tmPokerBridgeXhrHooked__) {
            return;
        }
        var OriginalXhr = target.XMLHttpRequest;
        if (typeof OriginalXhr !== 'function' || !OriginalXhr.prototype) {
            return;
        }
        var originalOpen = OriginalXhr.prototype.open;
        var originalSend = OriginalXhr.prototype.send;
        if (typeof originalOpen !== 'function' || typeof originalSend !== 'function') {
            return;
        }
        target.__tmPokerBridgeXhrHooked__ = true;

        OriginalXhr.prototype.open = function (method, url) {
            try {
                this.__tmPokerBridgeMethod = typeof method === 'string' ? method.toUpperCase() : String(method);
                this.__tmPokerBridgeUrl = String(url);
            } catch (_) {}
            return originalOpen.apply(this, arguments);
        };

        OriginalXhr.prototype.send = function (body) {
            var xhr = this;
            var method = xhr.__tmPokerBridgeMethod || 'GET';
            var url = xhr.__tmPokerBridgeUrl || '[unknown-xhr-url]';
            sendDiscoveryLine(sourceLabel, 'XHR_REQUEST', [method, url, summarizeBody(body)]);
            try {
                xhr.addEventListener('loadend', function () {
                    var contentType = '-';
                    try {
                        contentType = xhr.getResponseHeader('content-type') || '-';
                    } catch (_) {}
                    sendDiscoveryLine(sourceLabel, 'XHR_RESPONSE', [method, url, xhr.status, contentType]);
                    try {
                        if (typeof xhr.responseText === 'string' && /json|text|javascript|xml/i.test(contentType)) {
                            sendDiscoveryLine(sourceLabel, 'XHR_BODY', [method, url, truncateDiscoveryText(xhr.responseText)]);
                        }
                    } catch (_) {}
                });
            } catch (_) {}
            return originalSend.apply(this, arguments);
        };
        sendDebug('hooked xhr discovery source=' + sourceLabel);
    }

    function attachMessageEndpoint(endpoint, sourceLabel, endpointLabel) {
        if (!endpoint || endpoint.__tmPokerBridgeMessageEndpointHooked__) {
            return;
        }
        endpoint.__tmPokerBridgeMessageEndpointHooked__ = true;
        try {
            endpoint.addEventListener('message', function (event) {
                sendDiscoveryLine(sourceLabel, endpointLabel + '_MESSAGE', [
                    summarizeBody(event ? event.data : null)
                ]);
            });
        } catch (_) {}

        var originalPostMessage = endpoint.postMessage;
        if (typeof originalPostMessage === 'function') {
            try {
                endpoint.postMessage = function (message) {
                    sendDiscoveryLine(sourceLabel, endpointLabel + '_POST', [summarizeBody(message)]);
                    return originalPostMessage.apply(this, arguments);
                };
            } catch (_) {}
        }
    }

    function installWorkerDiscovery(target, sourceLabel) {
        if (!target || !isDiscoveryHost() || target.__tmPokerBridgeWorkerHooked__) {
            return;
        }
        target.__tmPokerBridgeWorkerHooked__ = true;

        var OriginalWorker = target.Worker;
        if (typeof OriginalWorker === 'function') {
            function WrappedWorker(scriptUrl, options) {
                var worker = options === undefined
                    ? new OriginalWorker(scriptUrl)
                    : new OriginalWorker(scriptUrl, options);
                sendDiscoveryLine(sourceLabel, 'WORKER_CREATE', [String(scriptUrl), summarizeBody(options)]);
                attachMessageEndpoint(worker, sourceLabel, 'WORKER');
                return worker;
            }
            WrappedWorker.prototype = OriginalWorker.prototype;
            try {
                Object.setPrototypeOf(WrappedWorker, OriginalWorker);
            } catch (_) {}
            target.Worker = WrappedWorker;
        }

        var OriginalSharedWorker = target.SharedWorker;
        if (typeof OriginalSharedWorker === 'function') {
            function WrappedSharedWorker(scriptUrl, options) {
                var worker = options === undefined
                    ? new OriginalSharedWorker(scriptUrl)
                    : new OriginalSharedWorker(scriptUrl, options);
                sendDiscoveryLine(sourceLabel, 'SHARED_WORKER_CREATE', [String(scriptUrl), summarizeBody(options)]);
                if (worker && worker.port) {
                    attachMessageEndpoint(worker.port, sourceLabel, 'SHARED_WORKER');
                }
                return worker;
            }
            WrappedSharedWorker.prototype = OriginalSharedWorker.prototype;
            try {
                Object.setPrototypeOf(WrappedSharedWorker, OriginalSharedWorker);
            } catch (_) {}
            target.SharedWorker = WrappedSharedWorker;
        }
        sendDebug('hooked worker discovery source=' + sourceLabel);
    }

    function installWindowMessageDiscovery(target, sourceLabel) {
        if (!target || !shouldDiscoverIframes() || target.__tmPokerBridgeWindowMessageHooked__) {
            return;
        }
        target.__tmPokerBridgeWindowMessageHooked__ = true;
        try {
            target.addEventListener('message', function (event) {
                var origin = event && typeof event.origin === 'string' ? event.origin : '-';
                sendDiscoveryLine(sourceLabel, 'WINDOW_MESSAGE', [origin, summarizeBody(event ? event.data : null)]);
            }, true);
            sendDebug('hooked window message discovery source=' + sourceLabel);
        } catch (_) {}
    }

    function tryHookAllSources() {
        try {
            hookConsole(window.console, 'sandbox-window');
        } catch (_) {}

        try {
            if (typeof unsafeWindow !== 'undefined' && unsafeWindow && unsafeWindow.console) {
                hookConsole(unsafeWindow.console, 'unsafeWindow');
            }
        } catch (_) {}

        try {
            installGlobalErrorCapture(window, 'sandbox-window');
        } catch (_) {}

        try {
            if (typeof unsafeWindow !== 'undefined' && unsafeWindow) {
                installGlobalErrorCapture(unsafeWindow, 'unsafeWindow');
            }
        } catch (_) {}

        try {
            if (typeof unsafeWindow !== 'undefined' && unsafeWindow) {
                installIframeDiscovery(unsafeWindow, 'unsafeWindow');
                installWebSocketDiscovery(unsafeWindow, 'unsafeWindow');
                installFetchDiscovery(unsafeWindow, 'unsafeWindow');
                installXhrDiscovery(unsafeWindow, 'unsafeWindow');
                installWorkerDiscovery(unsafeWindow, 'unsafeWindow');
                installWindowMessageDiscovery(unsafeWindow, 'unsafeWindow');
            }
        } catch (_) {}

        try {
            installIframeDiscovery(window, 'sandbox-window');
            installWebSocketDiscovery(window, 'sandbox-window');
            installFetchDiscovery(window, 'sandbox-window');
            installXhrDiscovery(window, 'sandbox-window');
            installWorkerDiscovery(window, 'sandbox-window');
            installWindowMessageDiscovery(window, 'sandbox-window');
        } catch (_) {}
    }

    // Public helper for console/manual automation.
    // Examples:
    // window.tmPokerBridge.send({ hole: ['Ah', 'Kd'] });
    // window.tmPokerBridge.send({ board: ['7c', '8d', '9h'] });
    // window.tmPokerBridge.send({ hole: ['Ah', 'Kd'], board: ['7c', '8d', '9h', 'Ts', 'Jd'] });
    var publicBridgeApi = {
        send: sendPokerCards,
        ping: function () {
            sendDebug('manual bridge test from ' + window.location.href);
            localDiagnostic('info', 'manual bridge test queued');
        }
    };
    window.tmPokerBridge = publicBridgeApi;
    try {
        if (typeof unsafeWindow !== 'undefined' && unsafeWindow) {
            unsafeWindow.tmPokerBridge = publicBridgeApi;
        }
    } catch (_) {}

    try {
        if (typeof GM_registerMenuCommand === 'function') {
            GM_registerMenuCommand('Test PokerOdds bridge', publicBridgeApi.ping);
        }
    } catch (_) {}

    localDiagnostic('info', 'loaded on ' + window.location.href);
    tryHookAllSources();

    try {
        window.setInterval(tryHookAllSources, REHOOK_INTERVAL_MS);
    } catch (_) {}

    sendDebug(
        'userscript loaded at ' + new Date().toISOString() +
        ' site=' + currentSiteKey() +
        ' rawMirror=' + ENABLE_RAW_MIRROR
    );
})();
    
// javascript sux
