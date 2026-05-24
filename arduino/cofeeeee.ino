// ═══════════════════════════════════════════════════════════════
// CaféBot — Arduino Uno R3 Slave Controller
// Receives JSON commands over USB Serial from Raspberry Pi
// Controls: 3 servos, 1 pump relay, 1 IR sensor, 1 buzzer
//
// IMPORTANT: Most relay modules are ACTIVE-LOW.
//   If your pump runs on boot → your relay IS active-low.
//   The defines below handle this correctly.
// ═══════════════════════════════════════════════════════════════

#include <Servo.h>
#include <ArduinoJson.h>

// ── Pin Definitions ──────────────────────────────────────────
#define IR_PIN      2    // IR sensor (LOW = cup present)
#define BUZZER_PIN  6    // Active buzzer (HIGH = beep)
#define PUMP_PIN    7    // Water pump relay control
#define COFFEE_PIN  9    // Coffee servo signal
#define MILK_PIN   10    // Milk servo signal
#define SUGAR_PIN  11    // Sugar servo signal

// ══════════════════════════════════════════════════════════════
// RELAY TYPE — Change these TWO lines if your relay is different
// ══════════════════════════════════════════════════════════════
// ACTIVE-LOW relay (most common — pump ran on boot = this is you):
#define PUMP_ON   LOW     // LOW  = relay energised = pump runs
#define PUMP_OFF  HIGH    // HIGH = relay released  = pump stops

// If you have an ACTIVE-HIGH relay (rare), uncomment these instead:
// #define PUMP_ON   HIGH
// #define PUMP_OFF  LOW
// ══════════════════════════════════════════════════════════════

// ── Timing Constants ─────────────────────────────────────────
#define SERVO_OPEN_ANGLE   90
#define SERVO_CLOSE_ANGLE   0
#define SERVO_HOLD_MS     600    // How long servo stays open per level
#define SERVO_GAP_MS      200    // Gap between levels
#define CUP_TIMEOUT_MS  30000    // 30 seconds to place cup
#define PUMP_ML_PER_MIN   100    // Pump flow rate: 100 ml/min

// ── Global Objects ───────────────────────────────────────────
Servo coffeeServo;
Servo milkServo;
Servo sugarServo;

// ── Stop Flag ────────────────────────────────────────────────
volatile bool stopFlag = false;

// ═══════════════════════════════════════════════════════════════
// SETUP
// ═══════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(9600);
  Serial.setTimeout(100);  // Non-blocking reads for STOP detection

  pinMode(IR_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(PUMP_PIN, OUTPUT);

  // ── CRITICAL: Turn pump OFF immediately on boot ──
  digitalWrite(PUMP_PIN, PUMP_OFF);
  digitalWrite(BUZZER_PIN, LOW);

  // Don't attach servos yet — attach only when dispensing
  // to prevent jitter and save power.

  delay(500);
  Serial.println("{\"status\":\"ready\"}");
}

// ═══════════════════════════════════════════════════════════════
// MAIN LOOP
// ═══════════════════════════════════════════════════════════════
void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  // Parse JSON command
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, line);

  if (err) {
    Serial.println("{\"status\":\"error\",\"msg\":\"json_parse_error\"}");
    return;
  }

  const char* cmd = doc["cmd"] | "";

  if (strcmp(cmd, "DISPENSE") == 0) {
    handleDispense(doc);
  }
  else if (strcmp(cmd, "TEST") == 0) {
    handleTest();
  }
  else if (strcmp(cmd, "STATUS") == 0) {
    handleStatus();
  }
  else if (strcmp(cmd, "BUZZ") == 0) {
    handleBuzz();
  }
  else if (strcmp(cmd, "STOP") == 0) {
    handleStop();
  }
  else {
    Serial.println("{\"status\":\"error\",\"msg\":\"unknown_command\"}");
  }
}

// ═══════════════════════════════════════════════════════════════
// CHECK FOR STOP (called frequently during dispensing)
// ═══════════════════════════════════════════════════════════════
bool checkStop() {
  if (stopFlag) return true;

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      StaticJsonDocument<128> doc;
      if (deserializeJson(doc, line) == DeserializationError::Ok) {
        const char* cmd = doc["cmd"] | "";
        if (strcmp(cmd, "STOP") == 0) {
          stopFlag = true;
          return true;
        }
      }
    }
  }
  return false;
}

// ═══════════════════════════════════════════════════════════════
// EMERGENCY STOP — all outputs OFF
// ═══════════════════════════════════════════════════════════════
void emergencyStop() {
  digitalWrite(PUMP_PIN, PUMP_OFF);    // Pump OFF (relay-safe)
  digitalWrite(BUZZER_PIN, LOW);       // Buzzer OFF

  coffeeServo.write(SERVO_CLOSE_ANGLE);
  milkServo.write(SERVO_CLOSE_ANGLE);
  sugarServo.write(SERVO_CLOSE_ANGLE);

  delay(100);
  coffeeServo.detach();
  milkServo.detach();
  sugarServo.detach();

  Serial.println("{\"status\":\"stopped\"}");
}

// ═══════════════════════════════════════════════════════════════
// HANDLE: DISPENSE
// ═══════════════════════════════════════════════════════════════
void handleDispense(StaticJsonDocument<256>& doc) {
  int coffeeLevel = doc["coffee"] | 0;
  int milkLevel   = doc["milk"]   | 0;
  int sugarLevel  = doc["sugar"]  | 0;
  int waterMl     = doc["water"]  | 0;

  // Validate
  coffeeLevel = constrain(coffeeLevel, 0, 5);
  milkLevel   = constrain(milkLevel,   0, 5);
  sugarLevel  = constrain(sugarLevel,  0, 5);
  waterMl     = constrain(waterMl,    80, 250);

  stopFlag = false;

  // ACK
  Serial.println("{\"status\":\"ack\",\"cmd\":\"DISPENSE\"}");

  // ── Wait for cup ───────────────────────────────────────────
  Serial.println("{\"status\":\"waiting_cup\"}");

  unsigned long cupStart = millis();
  bool cupFound = false;

  while (millis() - cupStart < CUP_TIMEOUT_MS) {
    if (checkStop()) { emergencyStop(); return; }

    if (digitalRead(IR_PIN) == LOW) {
      cupFound = true;
      break;
    }
    delay(50);
  }

  if (!cupFound) {
    Serial.println("{\"status\":\"error\",\"msg\":\"no_cup_timeout\"}");
    return;
  }

  Serial.println("{\"status\":\"cup_detected\"}");

  // Beep once — cup detected
  digitalWrite(BUZZER_PIN, HIGH);
  delay(100);
  digitalWrite(BUZZER_PIN, LOW);

  // ── Dispense: coffee, milk, sugar, water ───────────────────

  // Attach servos
  coffeeServo.attach(COFFEE_PIN);
  milkServo.attach(MILK_PIN);
  sugarServo.attach(SUGAR_PIN);

  // Reset to closed
  coffeeServo.write(SERVO_CLOSE_ANGLE);
  milkServo.write(SERVO_CLOSE_ANGLE);
  sugarServo.write(SERVO_CLOSE_ANGLE);
  delay(300);

  // Coffee
  if (coffeeLevel > 0) {
    if (!dispensePowder(coffeeServo, "coffee", coffeeLevel)) return;
  }

  // Milk
  if (milkLevel > 0) {
    if (!dispensePowder(milkServo, "milk", milkLevel)) return;
  }

  // Sugar
  if (sugarLevel > 0) {
    if (!dispensePowder(sugarServo, "sugar", sugarLevel)) return;
  }

  // Detach servos — done with powder
  coffeeServo.detach();
  milkServo.detach();
  sugarServo.detach();

  // Water
  if (waterMl > 0) {
    if (!dispenseWater(waterMl)) return;
  }

  // ── Done — beep twice ──────────────────────────────────────
  digitalWrite(BUZZER_PIN, HIGH);
  delay(100);
  digitalWrite(BUZZER_PIN, LOW);
  delay(100);
  digitalWrite(BUZZER_PIN, HIGH);
  delay(100);
  digitalWrite(BUZZER_PIN, LOW);

  Serial.println("{\"status\":\"done\"}");
}

// ═══════════════════════════════════════════════════════════════
// DISPENSE POWDER (one component)
// Returns false if STOP was received (already handled)
// ═══════════════════════════════════════════════════════════════
bool dispensePowder(Servo& servo, const char* name, int levels) {
  // Send 0% progress
  sendProgress(name, 0);

  for (int i = 1; i <= levels; i++) {
    if (checkStop()) { emergencyStop(); return false; }

    // Open
    servo.write(SERVO_OPEN_ANGLE);
    delay(SERVO_HOLD_MS);

    // Close
    servo.write(SERVO_CLOSE_ANGLE);

    // Send 50% at midpoint
    if (i == (levels + 1) / 2) {
      sendProgress(name, 50);
    }

    // Gap between levels
    if (i < levels) {
      delay(SERVO_GAP_MS);
    }
  }

  // Send 100%
  sendProgress(name, 100);
  return true;
}

// ═══════════════════════════════════════════════════════════════
// DISPENSE WATER (relay-aware)
// ═══════════════════════════════════════════════════════════════
bool dispenseWater(int ml) {
  unsigned long pumpOnMs = (unsigned long)((float)ml / PUMP_ML_PER_MIN * 60.0 * 1000.0);

  sendProgress("water", 0);

  if (checkStop()) { emergencyStop(); return false; }

  digitalWrite(PUMP_PIN, PUMP_ON);     // Pump ON (relay-safe)
  unsigned long pumpStart = millis();

  while (millis() - pumpStart < pumpOnMs) {
    if (checkStop()) {
      digitalWrite(PUMP_PIN, PUMP_OFF); // Pump OFF immediately
      emergencyStop();
      return false;
    }
    delay(100);
  }

  digitalWrite(PUMP_PIN, PUMP_OFF);    // Pump OFF (relay-safe)
  sendProgress("water", 100);
  return true;
}

// ═══════════════════════════════════════════════════════════════
// SEND PROGRESS JSON
// ═══════════════════════════════════════════════════════════════
void sendProgress(const char* component, int pct) {
  Serial.print("{\"status\":\"dispensing\",\"component\":\"");
  Serial.print(component);
  Serial.print("\",\"pct\":");
  Serial.print(pct);
  Serial.println("}");
}

// ═══════════════════════════════════════════════════════════════
// HANDLE: TEST
// ═══════════════════════════════════════════════════════════════
void handleTest() {
  // Quick buzzer beep to confirm hardware
  digitalWrite(BUZZER_PIN, HIGH);
  delay(100);
  digitalWrite(BUZZER_PIN, LOW);

  Serial.println("{\"status\":\"ok\",\"msg\":\"test_ok\"}");
}

// ═══════════════════════════════════════════════════════════════
// HANDLE: STATUS
// ═══════════════════════════════════════════════════════════════
void handleStatus() {
  bool cupPresent = (digitalRead(IR_PIN) == LOW);
  Serial.print("{\"status\":\"ok\",\"connected\":true,\"cup\":");
  Serial.print(cupPresent ? "true" : "false");
  Serial.println("}");
}

// ═══════════════════════════════════════════════════════════════
// HANDLE: BUZZ
// ═══════════════════════════════════════════════════════════════
void handleBuzz() {
  digitalWrite(BUZZER_PIN, HIGH);
  delay(200);
  digitalWrite(BUZZER_PIN, LOW);

  Serial.println("{\"status\":\"ok\",\"msg\":\"buzz_done\"}");
}

// ═══════════════════════════════════════════════════════════════
// HANDLE: STOP
// ═══════════════════════════════════════════════════════════════
void handleStop() {
  stopFlag = true;
  emergencyStop();
}
