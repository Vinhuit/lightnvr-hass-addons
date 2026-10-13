/* Keep go2rtc transport URLs on the authenticated Home Assistant origin. */
(() => {
    const prefix = location.pathname.match(/^\/api\/(?:hassio_ingress|ingress)\/[^/?#]+/)?.[0];
    if (!prefix) return;
    function mapUrl(input) {
        try {
            const url = new URL(input, location.href);
            if (url.hostname !== location.hostname || !url.pathname.startsWith('/go2rtc/')) return input;
            url.protocol = url.protocol === 'ws:' || url.protocol === 'wss:'
                ? (location.protocol === 'https:' ? 'wss:' : 'ws:')
                : location.protocol;
            url.host = location.host;
            url.pathname = prefix + url.pathname;
            return url.href;
        } catch {
            return input;
        }
    }
    const fetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
        if (input instanceof Request) return fetch(new Request(mapUrl(input.url), input), init);
        return fetch(mapUrl(input), init);
    };
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        return open.call(this, method, mapUrl(url), ...args);
    };
    window.WebSocket = new Proxy(window.WebSocket, {
        construct(target, args) {
            args[0] = mapUrl(args[0]);
            return Reflect.construct(target, args);
        }
    });
})();
