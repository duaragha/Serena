# Tasker Setup Guide (Samsung Galaxy S23 Ultra)

You need Tasker + AutoNotification APKs installed.

## 1. SMS Interception (for "Hey Google, text myself" reminders)

When you text yourself via Google Assistant while driving, Tasker catches
the SMS and forwards it to the reminder daemon via ntfy.sh.

### Profile
- **Trigger**: Event > Phone > Received Text
- **Sender**: your own phone number
- **Content**: (leave blank — catch all)

### Task: Forward SMS to Daemon
1. **HTTP Request**
   - Method: POST
   - URL: `https://ntfy.sh/YOUR_INPUT_TOPIC`
   - Headers: `Title: reminder`
   - Body: `%SMSRB`
   - (Replace YOUR_INPUT_TOPIC with your NTFY_INPUT_TOPIC from .env)

---

## 2. Payment Detection (Google Wallet notifications)

When Google Wallet sends a payment notification, Tasker tells the daemon
to fire all pending "when I pay" reminders.

### Prerequisites
- Install **AutoNotification** APK
- Grant AutoNotification notification access in Android Settings > Notifications > Notification access

### Profile
- **Trigger**: Plugin > AutoNotification > Intercept
- **App**: Google Wallet (com.google.android.apps.walletnfcrel)
- **Title Filter**: (leave blank — catch all wallet notifications)

### Task: Send Payment Event
1. **HTTP Request**
   - Method: POST
   - URL: `https://ntfy.sh/YOUR_INPUT_TOPIC`
   - Headers: `Title: payment`
   - Body: `Payment detected: %antitle`
   - (Replace YOUR_INPUT_TOPIC with your NTFY_INPUT_TOPIC from .env)

---

## 3. Voice Input Widget (optional — for when Assistant is unreliable)

A home screen widget that you tap once to speak a reminder.

### Task: Voice Reminder
1. **Get Voice** (under Input)
   - Title: "Reminder"
   - Language Model: free_form
2. **HTTP Request**
   - Method: POST
   - URL: `https://ntfy.sh/YOUR_INPUT_TOPIC`
   - Headers: `Title: reminder`
   - Body: `%VOICE`

### Widget
- Long-press home screen > Widgets > Tasker > Task Shortcut
- Select the "Voice Reminder" task
- Pick an icon (bell, megaphone, etc.)

---

## 4. ntfy.sh App Setup (for receiving reminder alerts)

1. Install **ntfy** from F-Droid or APK
2. Open ntfy > tap + > Subscribe
3. Topic: your NTFY_ALERT_TOPIC from .env
4. Server: https://ntfy.sh
5. Tap the subscription > Settings icon (top right):
   - **Min priority**: 5 (Maximum)
   - Enable "Instant delivery"
6. Go to Android Settings > Apps > ntfy > Notifications:
   - Find the "Max priority" channel
   - Enable **Override Do Not Disturb**
   - Set a loud ringtone/alarm sound
   - Set vibration pattern to continuous

---

## Quick Test

After setup, test each piece:

```bash
# Test ntfy alert (should make your phone ring)
curl -H "Priority: 5" -H "Title: TEST" -d "This is a test reminder" ntfy.sh/YOUR_ALERT_TOPIC

# Test payment event (should fire any pending payment reminders)
curl -H "Title: payment" -d "Test payment" ntfy.sh/YOUR_INPUT_TOPIC

# Test new reminder via input topic
curl -H "Title: reminder" -d "remind me to buy milk at 7pm" ntfy.sh/YOUR_INPUT_TOPIC
```
