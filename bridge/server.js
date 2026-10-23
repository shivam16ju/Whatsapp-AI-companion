import express from 'express';
import makeWASocket, {
  Browsers,
  useMultiFileAuthState,
  makeCacheableSignalKeyStore,
  DisconnectReason,
  fetchLatestBaileysVersion,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcode from 'qrcode';
import qrcodeTerminal from 'qrcode-terminal';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

process.on('uncaughtException', (err) => {
  console.warn('[Bridge] Caught uncaughtException:', err?.message || err);
});

process.on('unhandledRejection', (reason) => {
  console.warn('[Bridge] Caught unhandledRejection:', reason?.message || reason);
});

process.on('exit', (code) => {
  console.log(`[Bridge] Process exiting with code: ${code}`);
});

const app = express();
app.use(express.json());

const PORT = process.env.BRIDGE_PORT || 3001;
const PYTHON_WEBHOOK_URL = process.env.PYTHON_WEBHOOK_URL || 'http://127.0.0.1:8000/api/bridge/webhook';
const AUTH_DIR = path.join(__dirname, 'bridge_auth');

let sock = null;
let connectionStatus = 'initializing'; // 'initializing', 'qr', 'connected', 'disconnected'
let currentQRDataUrl = null;
let currentRawQR = null;
let connectedUser = null;
let isStarting = false;
const botSentMessageIds = new Set();
const messageStore = new Map();

async function startWhatsApp() {
  if (isStarting) return;
  isStarting = true;

  try {
    if (!fs.existsSync(AUTH_DIR)) {
      fs.mkdirSync(AUTH_DIR, { recursive: true });
    }
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    let version = [2, 3000, 1015901307];
    let isLatest = true;
    try {
      const vInfo = await fetchLatestBaileysVersion();
      version = vInfo.version;
      isLatest = vInfo.isLatest;
    } catch (e) {
      console.warn('[Bridge] Using default WA version:', e.message);
    }
    console.log(`[Bridge] Starting WA client v${version.join('.')}, isLatest: ${isLatest}`);

    const bridgeLogger = pino({ level: 'silent' });

    sock = makeWASocket({
      version,
      logger: bridgeLogger,
      printQRInTerminal: false,
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, bridgeLogger),
      },
      browser: Browsers.macOS('Desktop'),
      syncFullHistory: false, // Prevents loading massive past chats that cause timeouts/conflicts
      markOnlineOnConnect: true,
      defaultQueryTimeoutMs: 60000,
      getMessage: async (key) => {
        return messageStore.get(key.id) || undefined;
      },
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        currentRawQR = qr;
        connectionStatus = 'qr';
        try {
          currentQRDataUrl = await qrcode.toDataURL(qr);
        } catch (e) {
          console.error('[Bridge] Error generating QR Data URL:', e);
        }
        console.log('\n======================================================');
        console.log('SCAN THIS QR CODE IN WHATSAPP (Linked Devices -> Link a Device):');
        console.log('======================================================\n');
        qrcodeTerminal.generate(qr, { small: true });
        console.log('\nOr view in browser at: http://localhost:3001/qr\n');
      }

      if (connection === 'close') {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const errorMessage = lastDisconnect?.error?.message || '';
        connectionStatus = 'disconnected';
        connectedUser = null;
        currentQRDataUrl = null;
        currentRawQR = null;
        isStarting = false;

        console.log(`[Bridge] Connection closed. StatusCode: ${statusCode}, Error: ${errorMessage}`);

        // If logged out or session replaced, wipe auth dir
        if (statusCode === DisconnectReason.loggedOut || statusCode === 440) {
          console.log('[Bridge] Session logged out or replaced. Clearing auth credentials...');
          try {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
            fs.mkdirSync(AUTH_DIR, { recursive: true });
          } catch (err) {
            console.error('[Bridge] Failed to delete auth dir:', err);
          }
        }

        console.log('[Bridge] Restarting WhatsApp client in 2.5 seconds...');
        setTimeout(() => {
          isStarting = false;
          startWhatsApp();
        }, 2500);

      } else if (connection === 'open') {
        connectionStatus = 'connected';
        currentQRDataUrl = null;
        currentRawQR = null;
        connectedUser = sock.user;
        isStarting = false;
        console.log('\n======================================================');
        console.log(`[Bridge] WhatsApp Connected Successfully as ${sock.user?.id || 'Unknown'}!`);
        console.log('======================================================\n');

        // Automatically notify backend to check and reply to any unreplied left-out messages
        fetch('http://127.0.0.1:8000/api/bridge/recover-pending', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        }).catch((err) => {
          console.warn('[Bridge] Auto-recover trigger warning:', err.message);
        });
      }
    });

    // Helper to sync detected WhatsApp contacts and chats into Python backend
    async function syncContactsToBackend(contactsList) {
      if (!contactsList || contactsList.length === 0) return;
      const items = [];
      for (const item of contactsList) {
        const jid = item.id || item.jid || '';
        if (!jid || jid.endsWith('@g.us') || jid.endsWith('@broadcast') || jid.endsWith('@newsletter')) continue;
        let number = jid.split('@')[0].split(':')[0].replace(/\D/g, '');
        if (jid.endsWith('@lid')) {
          try {
            if (sock.signalRepository?.lidMapping?.getPNForLID) {
              const pn = await sock.signalRepository.lidMapping.getPNForLID(jid);
              if (pn) number = pn.split('@')[0].split(':')[0].replace(/\D/g, '');
            }
          } catch (e) {}
        }
        if (!number || number.length < 7) continue;
        const name = item.name || item.notify || item.verifiedName || number;
        items.push({ number, name, jid });
      }

      if (items.length > 0) {
        try {
          await fetch('http://127.0.0.1:8000/api/bridge/sync-contacts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contacts: items }),
          });
          console.log(`[Bridge] Synced ${items.length} contacts/chats to backend.`);
        } catch (err) {
          console.warn('[Bridge] Failed to sync contacts to backend:', err.message);
        }
      }
    }

    sock.ev.on('contacts.upsert', (contacts) => syncContactsToBackend(contacts));
    sock.ev.on('contacts.update', (updates) => syncContactsToBackend(updates));
    sock.ev.on('chats.upsert', (chats) => syncContactsToBackend(chats));
    sock.ev.on('chats.update', (updates) => syncContactsToBackend(updates));
    sock.ev.on('messaging-history.set', ({ chats, contacts, messages }) => {
      const all = [...(chats || []), ...(contacts || [])];
      syncContactsToBackend(all);
    });

    // Handle incoming & outgoing messages (both real-time notify and offline append)
    sock.ev.on('messages.upsert', async (m) => {
      if (m.type !== 'notify' && m.type !== 'append') return;

      for (const msg of m.messages) {
        if (!msg.key || !msg.key.id) continue;
        if (botSentMessageIds.has(msg.key.id)) continue;

        // Skip messages older than 24h to avoid spamming ancient history
        const msgTs = Number(msg.messageTimestamp || 0);
        const nowSec = Math.floor(Date.now() / 1000);
        if (msgTs > 0 && (nowSec - msgTs) > 86400) continue;

        const remoteJid = msg.key.remoteJid || '';
        if (remoteJid.endsWith('@broadcast') || remoteJid.endsWith('@newsletter')) continue;
        if (!remoteJid.endsWith('@s.whatsapp.net') && !remoteJid.endsWith('@lid')) continue;

        let senderNumber = remoteJid.split('@')[0].split(':')[0].replace(/\D/g, '');

        // If it's a LID, attempt to resolve phone number
        if (remoteJid.endsWith('@lid')) {
          try {
            if (sock.signalRepository?.lidMapping?.getPNForLID) {
              const resolvedPN = await sock.signalRepository.lidMapping.getPNForLID(remoteJid);
              if (resolvedPN) {
                const pnOnly = resolvedPN.split('@')[0].split(':')[0].replace(/\D/g, '');
                if (pnOnly) {
                  console.log(`[Bridge] Resolved LID ${remoteJid} -> phone ${pnOnly}`);
                  senderNumber = pnOnly;
                }
              }
            }
          } catch (e) {
            console.warn('[Bridge] LID lookup warning:', e.message);
          }
        }

        const pushName = msg.pushName || senderNumber;
        const myJid = sock.user?.id || '';
        const myPhone = myJid.split(':')[0].replace(/\D/g, '');
        const myLid = sock.user?.lid || '';
        const isSelfChat = Boolean(
          (myPhone && senderNumber === myPhone) ||
          (myLid && remoteJid.includes(myLid.split(':')[0]))
        );
        const isFromMe = Boolean(msg.key.fromMe && !isSelfChat);

        let messageContent = msg.message;
        if (!messageContent) continue;

        // Unpack wrappers (ephemeral, viewOnce, etc.)
        if (messageContent.ephemeralMessage?.message) {
          messageContent = messageContent.ephemeralMessage.message;
        }
        if (messageContent.viewOnceMessage?.message) {
          messageContent = messageContent.viewOnceMessage.message;
        }
        if (messageContent.viewOnceMessageV2?.message) {
          messageContent = messageContent.viewOnceMessageV2.message;
        }
        if (messageContent.documentWithCaptionMessage?.message) {
          messageContent = messageContent.documentWithCaptionMessage.message;
        }

        messageStore.set(msg.key.id, messageContent);
        if (messageStore.size > 200) {
          const firstKey = messageStore.keys().next().value;
          messageStore.delete(firstKey);
        }

        let text = '';
        if (messageContent.conversation) {
          text = messageContent.conversation;
        } else if (messageContent.extendedTextMessage?.text) {
          text = messageContent.extendedTextMessage.text;
        } else if (messageContent.imageMessage?.caption) {
          text = messageContent.imageMessage.caption;
        } else if (messageContent.videoMessage?.caption) {
          text = messageContent.videoMessage.caption;
        }

        if (!text || !text.trim()) continue;

        if (isFromMe) {
          console.log(`[Bridge] Outbound message (from phone) to ${senderNumber} [${remoteJid}]: "${text}"`);
        } else {
          console.log(`[Bridge] Inbound message from ${pushName} (${senderNumber}) [${remoteJid}]: "${text}"`);
        }

        // Forward to Python backend
        try {
          const response = await fetch(PYTHON_WEBHOOK_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              sender: senderNumber,
              name: isFromMe ? '' : pushName,
              text: text.trim(),
              message_id: msg.key.id,
              jid: remoteJid,
              from_me: isFromMe,
            }),
          });
          if (!response.ok) {
            console.error(`[Bridge] Python webhook responded with ${response.status}: ${await response.text()}`);
          }
        } catch (err) {
          console.error('[Bridge] Failed to forward message to Python webhook:', err.message);
        }
      }
    });

  } catch (err) {
    console.error('[Bridge] Error in startWhatsApp:', err);
    isStarting = false;
    setTimeout(startWhatsApp, 3000);
  }
}

// REST Endpoints
app.get('/status', (req, res) => {
  res.json({
    status: connectionStatus,
    user: connectedUser,
    has_qr: !!currentQRDataUrl,
    qr: currentQRDataUrl,
  });
});

app.post('/reset', (req, res) => {
  console.log('[Bridge] Manual reset requested. Clearing auth and restarting...');
  try {
    if (sock) sock.end();
  } catch (e) {}
  try {
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    fs.mkdirSync(AUTH_DIR, { recursive: true });
  } catch (e) {}
  connectionStatus = 'initializing';
  currentQRDataUrl = null;
  connectedUser = null;
  isStarting = false;
  setTimeout(startWhatsApp, 1000);
  res.json({ success: true, message: 'Resetting WhatsApp session' });
});

app.get('/qr', (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html>
    <head>
      <title>Link WhatsApp Companion</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #0f172a;
          color: #f8fafc;
          text-align: center;
          padding: 40px 20px;
          margin: 0;
        }
        .card {
          max-width: 440px;
          margin: 0 auto;
          background: #1e293b;
          padding: 30px;
          border-radius: 16px;
          box-shadow: 0 10px 25px rgba(0,0,0,0.5);
          border: 1px solid #334155;
        }
        img#qrImg {
          border-radius: 12px;
          background: white;
          padding: 12px;
          max-width: 280px;
          width: 100%;
          display: inline-block;
          box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        ol {
          text-align: left;
          margin: 20px 0;
          line-height: 1.6;
          font-size: 14px;
          color: #cbd5e1;
        }
        .badge {
          background: #334155;
          padding: 4px 12px;
          border-radius: 999px;
          font-size: 12px;
          display: inline-block;
          margin-bottom: 12px;
          color: #94a3b8;
        }
        .status-box {
          margin: 15px 0;
          font-size: 15px;
          font-weight: 500;
        }
        .btn {
          background: #334155;
          color: #f8fafc;
          border: 1px solid #475569;
          padding: 8px 16px;
          border-radius: 8px;
          cursor: pointer;
          font-size: 13px;
          transition: background 0.2s;
        }
        .btn:hover { background: #475569; }
        .spinner {
          display: inline-block;
          width: 32px;
          height: 32px;
          border: 3px solid rgba(255,255,255,0.2);
          border-radius: 50%;
          border-top-color: #38bdf8;
          animation: spin 1s ease-in-out infinite;
          margin: 20px 0;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
      </style>
    </head>
    <body>
      <div class="card">
        <span class="badge">&#128241; WhatsApp Web Bridge</span>
        <h2 id="title">Link WhatsApp</h2>

        <div id="instructions">
          <ol>
            <li>Open <strong>WhatsApp</strong> on your phone</li>
            <li>Tap <strong>Settings</strong> &gt; <strong>Linked Devices</strong></li>
            <li>Tap <strong>Link a Device</strong> and point your camera here</li>
          </ol>
        </div>

        <div id="qrContainer">
          <div class="spinner"></div>
          <p id="loadingText" style="color: #94a3b8; font-size: 13px;">Generating QR Code...</p>
        </div>

        <div id="connectedContainer" style="display: none; padding: 20px 0;">
          <h2 style="color: #22c55e; margin-bottom: 8px;">&#10004; Connected!</h2>
          <p style="color: #94a3b8; font-size: 14px;">Your WhatsApp companion is active and auto-replying.</p>
          <p id="connectedUser" style="font-weight: 600; color: #f8fafc;"></p>
          <a href="/" class="btn" style="display: inline-block; background: #22c55e; color: #000; font-weight: 600; text-decoration: none; margin-top: 15px;">Open Dashboard</a>
        </div>

        <div style="margin-top: 25px; border-top: 1px solid #334155; padding-top: 15px;">
          <button class="btn" onclick="resetSession()">&#128260; Reset / Generate New QR</button>
        </div>
      </div>

      <script>
        let isConnected = false;

        async function checkStatus() {
          try {
            const res = await fetch('/status');
            const data = await res.json();

            if (data.status === 'connected') {
              isConnected = true;
              document.getElementById('instructions').style.display = 'none';
              document.getElementById('qrContainer').style.display = 'none';
              document.getElementById('connectedContainer').style.display = 'block';
              document.getElementById('title').innerText = 'WhatsApp Connected';
              const phone = data.user?.id ? data.user.id.split(':')[0] : 'Active';
              document.getElementById('connectedUser').innerText = 'Linked Phone: +' + phone;
              return;
            }

            if (data.status === 'qr' && data.qr) {
              document.getElementById('instructions').style.display = 'block';
              document.getElementById('connectedContainer').style.display = 'none';
              document.getElementById('title').innerText = 'Link WhatsApp';
              document.getElementById('qrContainer').innerHTML =
                '<img id="qrImg" src="' + data.qr + '" alt="QR Code" /><p style="font-size: 12px; color: #94a3b8; margin-top: 12px;">Point camera to scan</p>';
            } else {
              if (!isConnected) {
                document.getElementById('qrContainer').innerHTML =
                  '<div class="spinner"></div><p style="color: #94a3b8; font-size: 13px;">Preparing QR code, please wait a moment...</p>';
              }
            }
          } catch (e) {
            console.error(e);
          }
        }

        async function resetSession() {
          if (!confirm('Are you sure you want to reset and generate a fresh QR code?')) return;
          document.getElementById('qrContainer').innerHTML =
            '<div class="spinner"></div><p style="color: #94a3b8; font-size: 13px;">Resetting session...</p>';
          await fetch('/reset', { method: 'POST' });
          setTimeout(checkStatus, 1500);
        }

        setInterval(checkStatus, 2000);
        checkStatus();
      </script>
    </body>
    </html>
  `);
});

app.post('/send', async (req, res) => {
  const { to, text, jid: directJid, gifUrl, stickerUrl, imageUrl } = req.body;
  if (!to && !directJid) {
    return res.status(400).json({ error: 'Missing "to" or "jid" in body' });
  }

  if (connectionStatus !== 'connected' || !sock) {
    return res.status(503).json({ error: 'WhatsApp is not connected. Scan QR code first.' });
  }

  try {
    let targetJid = directJid;
    if (!targetJid) {
      const clean = String(to).trim();
      if (clean.includes('@')) {
        targetJid = clean;
      } else if (clean.length >= 14 && !clean.startsWith('91') && !clean.startsWith('1')) {
        targetJid = `${clean}@lid`;
      } else {
        const digits = clean.replace(/\D/g, '');
        targetJid = `${digits}@s.whatsapp.net`;
      }
    }

    let result;
    if (stickerUrl) {
      try {
        result = await sock.sendMessage(targetJid, {
          sticker: { url: stickerUrl },
        });
        if (text && text.trim()) {
          await sock.sendMessage(targetJid, { text: text.trim() });
        }
      } catch (stkErr) {
        console.warn(`[Bridge] Failed to send sticker (${stkErr.message}), falling back to text:`, text);
        result = await sock.sendMessage(targetJid, { text: text || '😎' });
      }
    } else if (imageUrl) {
      try {
        let mediaPayload;
        if (typeof imageUrl === 'string' && fs.existsSync(imageUrl)) {
          mediaPayload = { image: fs.readFileSync(imageUrl), caption: text || undefined };
        } else {
          mediaPayload = { image: { url: imageUrl }, caption: text || undefined };
        }
        result = await sock.sendMessage(targetJid, mediaPayload);
      } catch (imgErr) {
        console.warn(`[Bridge] Failed to send image (${imgErr.message}), falling back to text:`, text);
        result = await sock.sendMessage(targetJid, { text: text || '👀' });
      }
    } else if (gifUrl) {
      try {
        result = await sock.sendMessage(targetJid, {
          video: { url: gifUrl },
          gifPlayback: true,
          caption: text || undefined,
        });
      } catch (mediaErr) {
        console.warn(`[Bridge] Failed to send GIF (${mediaErr.message}), falling back to text:`, text);
        result = await sock.sendMessage(targetJid, { text: text || '👀' });
      }
    } else {
      result = await sock.sendMessage(targetJid, { text });
    }

    if (result?.key?.id) {
      botSentMessageIds.add(result.key.id);
      if (botSentMessageIds.size > 2000) {
        const first = botSentMessageIds.values().next().value;
        botSentMessageIds.delete(first);
      }
    }
    console.log(`[Bridge] Sent message to ${targetJid}: "${text || '[GIF]'}"`);
    res.json({ success: true, message_id: result?.key?.id });
  } catch (err) {
    console.error(`[Bridge] Failed to send message to ${to}:`, err);
    res.status(500).json({ error: err.message });
  }
});

// Start Express and WhatsApp
app.listen(PORT, '127.0.0.1', () => {
  console.log(`[Bridge] Server running on http://127.0.0.1:${PORT}`);
  startWhatsApp();
});
