// ==UserScript==
// @name         PokerOdds Tagged Bridge
// @namespace    http://tampermonkey.net/
// @version      1.3
// @description  Forward explicitly tagged poker messages to local PokerOdds bridge
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// ==/UserScript==

(function () {
    'use strict';

    var BRIDGE_URLS = ['http://127.0.0.1:5000/log', 'http://localhost:5000/log'];
    var currentBridgeUrlIndex = 0;
    var BRIDGE_TAG = 'TM_BRIDGE:';
    var RAW_TAG = '[RAW_CONSOLE]';
    var ENABLE_RAW_MIRROR = true;
    var REHOOK_INTERVAL_MS = 1500;
    var lastSentKey = null;
    var HOOK_FLAG = '__tmPokerBridgeHooked__';
    var pendingLines = [];
    var flushTimer = null;
    var flushing = false;
    var retryDelayMs = 200;
    var MAX_QUEUE_SIZE = 5000;

    var handState = {
        handId: null,
        activeSeats: {},
        activeCount: null,
        boardCount: 0,
        holeSentForHand: false,
        heroUserId: null
    };

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
            headers: {
                'Content-Type': 'text/plain'
            },
            data: line,
            onload: function (res) {
                flushing = false;
                if (res.status === 200) {
                    pendingLines.shift();
                    retryDelayMs = 200;
                    if (pendingLines.length > 0) {
                        scheduleFlush(0);
                    }
                    return;
                }

                rotateBridgeUrl();
                retryDelayMs = Math.min(5000, retryDelayMs * 2);
                scheduleFlush(retryDelayMs);
            },
            onerror: function () {
                flushing = false;
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
        if (!ENABLE_RAW_MIRROR) {
            return;
        }
        sendLine(RAW_TAG + ' [' + source + ':' + level + '] ' + compactArgs(args));
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
        sendLine('[BRIDGE_DEBUG] ' + message);
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
        if (!/^[2-9TJQKA][cdhsCDHS]$/.test(cleaned)) {
            return null;
        }
        return cleaned[0].toUpperCase() + cleaned[1].toLowerCase();
    }

    function isValidCardCode(value) {
        return typeof value === 'string' && /^[2-9TJQKA][cdhsCDHS]$/.test(value.trim());
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
        if (!handState.heroUserId || !playerUserId || playerUserId !== handState.heroUserId) {
            return null;
        }

        var cards = extractCardsFromValue(value.cards, 2);
        if (!cards || cards.length !== 2) {
            return null;
        }

        handState.holeSentForHand = true;
        var payload = { type: 'poker_cards', hole: cards };
        if (playersCount !== null) {
            payload.players = playersCount;
        }
        return payload;
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
            var playersCount = rebuildActiveSeatsFromPlayers(value.players, handId, action);
            if (playersCount === null && isTrustedPlayersAction(action)) {
                playersCount = extractPlayersCount(value.players);
            }

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
                return startPayload;
            }

            if (action === 'authenticated') {
                var authenticatedUserId = extractUserId(value);
                if (authenticatedUserId) {
                    handState.heroUserId = authenticatedUserId;
                }
            }

            if (action === 'resetTable' || action === 'finishHand') {
                handState.boardCount = 0;
                handState.holeSentForHand = false;
            }

            if (action === 'fold') {
                var foldedCount = markSeatFolded(value.seatId, handId);
                if (foldedCount !== null) {
                    return { type: 'poker_cards', players: foldedCount };
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
                    return boardPayload;
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
                    return holePayload;
                }
                if (Array.isArray(value.players)) {
                    if (handState.heroUserId) {
                        for (var hp = 0; hp < value.players.length; hp += 1) {
                            var heroPlayer = value.players[hp];
                            if (extractUserId(heroPlayer) !== handState.heroUserId) {
                                continue;
                            }
                            var heroCards = heroPlayer && Array.isArray(heroPlayer.cards) ? extractCardsFromValue(heroPlayer.cards, 2) : null;
                            if (heroCards && heroCards.length === 2) {
                                handState.holeSentForHand = true;
                                var heroHolePayload = { type: 'poker_cards', hole: heroCards };
                                if (playersCount !== null) {
                                    heroHolePayload.players = playersCount;
                                }
                                return heroHolePayload;
                            }
                            break;
                        }
                    }

                    if (!handState.heroUserId) {
                        var visibleHands = [];
                        for (var p = 0; p < value.players.length; p += 1) {
                            var player = value.players[p];
                            if (player && Array.isArray(player.cards)) {
                                var playerCards = extractCardsFromValue(player.cards, 2);
                                if (playerCards && playerCards.length === 2) {
                                    visibleHands.push(playerCards);
                                }
                            }
                        }
                        if (visibleHands.length === 1) {
                            handState.holeSentForHand = true;
                            var playerHolePayload = { type: 'poker_cards', hole: visibleHands[0] };
                            if (playersCount !== null) {
                                playerHolePayload.players = playersCount;
                            }
                            return playerHolePayload;
                        }
                    }
                }
            }
                if (action === 'show' || action === 'showdown' || action === 'awardPot' || action === 'finishHand') {
                    if (playersCount !== null) {
                        return { type: 'poker_cards', players: playersCount };
                    }
                    return null;
                }

            if (playersCount !== null) {
                return { type: 'poker_cards', players: playersCount };
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
    }

    // Public helper for console/manual automation.
    // Examples:
    // window.tmPokerBridge.send({ hole: ['Ah', 'Kd'] });
    // window.tmPokerBridge.send({ board: ['7c', '8d', '9h'] });
    // window.tmPokerBridge.send({ hole: ['Ah', 'Kd'], board: ['7c', '8d', '9h', 'Ts', 'Jd'] });
    window.tmPokerBridge = {
        send: sendPokerCards,
        ping: function () {
            sendPayload({ type: 'poker_cards', board: [] });
        }
    };

    tryHookAllSources();

    try {
        window.setInterval(tryHookAllSources, REHOOK_INTERVAL_MS);
    } catch (_) {}

    sendDebug('userscript loaded at ' + new Date().toISOString());
})();
    