// Native Node WebSocket: public data only, no wallet or trading methods.
const report = (value) => process.stdout.write(JSON.stringify(value) + "\n");
const tokens = process.argv.slice(2);
const books = tokens.length > 0;
async function connect() {
  while (true) {
    await new Promise((resolve) => {
      const ws = new WebSocket(
        books
          ? "wss://ws-subscriptions-clob.polymarket.com/ws/market"
          : "wss://ws-live-data.polymarket.com",
      );
      let lastMessage = Date.now();
      const heartbeat = setInterval(() => {
        if (Date.now() - lastMessage > 20000) ws.close();
        else if (ws.readyState === WebSocket.OPEN) ws.send("PING");
      }, 5000);
      ws.onopen = () =>
        ws.send(
          JSON.stringify(
            books
              ? { assets_ids: tokens, type: "market" }
              : {
                  action: "subscribe",
                  subscriptions: [
                    { topic: "crypto_prices_twap_sixty", type: "update" },
                  ],
                },
          ),
        );
      ws.onmessage = (event) => {
        lastMessage = Date.now();
        try {
          const msg = JSON.parse(event.data);
          if (books || msg.topic === "crypto_prices_twap_sixty") report(msg);
        } catch {}
      };
      ws.onerror = () => {
        report({
          error: books
            ? "Orderbook connection error"
            : "Chainlink RTDS connection error",
        });
        ws.close();
      };
      ws.onclose = () => {
        clearInterval(heartbeat);
        report({
          error: books
            ? "Orderbook disconnected; reconnecting"
            : "Chainlink RTDS disconnected; reconnecting",
        });
        resolve();
      };
    });
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}
process.on("SIGTERM", () => process.exit(0));
process.stdout.on("error", () => process.exit(0));
connect();
