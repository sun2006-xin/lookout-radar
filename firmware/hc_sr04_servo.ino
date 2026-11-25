// 超声波 + 舵机 下位机固件
// 对应上位机驱动：radarscope/drivers/ultrasonic.py
//
// 接线（Arduino UNO / Nano，STM32 改对应的 GPIO 即可）
//   HC-SR04 VCC -> 5V      HC-SR04 GND -> GND
//   HC-SR04 TRIG -> D9     HC-SR04 ECHO -> D10
//   SG90 信号 -> D3        舵机电源一定要单独 5V 供电，共地
//
// 输出协议：每测一次发一行 ASCII，115200 8N1
//   #<角度>,<距离cm>\n      例如 #90,142
// 不要用 Serial.print 拼字符串发原始浮点，容易和上位机的行解析错位。

#include <Servo.h>

static const int PIN_TRIG = 9;
static const int PIN_ECHO = 10;
static const int PIN_SERVO = 3;

Servo g_servo;

// 舵机每步之间的稳定等待，毫秒。
// 太小会导致刚转到位就测距，读数抖动；太大会让一圈变慢。
static const int SETTLE_MS = 30;

// 单次测距，返回厘米；超出量程或超时返回 -1
float measure_cm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(3);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);

  // 20ms 超时约对应 3.4 米
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 20000UL);
  if (us == 0) return -1.0f;
  return us / 58.0f;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  g_servo.attach(PIN_SERVO);
}

void loop() {
  for (int a = 0; a <= 180; a += 2) {
    g_servo.write(a);
    delay(SETTLE_MS);

    float d = measure_cm();
    if (d > 0.0f) {
      Serial.print('#');
      Serial.print(a);
      Serial.print(',');
      Serial.println((int)d);
    }
  }
  for (int a = 180; a >= 0; a -= 2) {
    g_servo.write(a);
    delay(SETTLE_MS);

    float d = measure_cm();
    if (d > 0.0f) {
      Serial.print('#');
      Serial.print(a);
      Serial.print(',');
      Serial.println((int)d);
    }
  }
}
