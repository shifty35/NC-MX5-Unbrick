#include <avr/io.h>
#include <avr/interrupt.h>

void setup()
{
    // Leave UART pins high-impedance so the onboard USB-UART
    // can communicate directly with the ECU.
    pinMode(0, INPUT);
    pinMode(1, INPUT);

    // D12 = PB4
    // D13 = PB5 / onboard LED
    pinMode(12, OUTPUT);
    pinMode(13, OUTPUT);

    digitalWrite(12, LOW);
    digitalWrite(13, LOW);

    // Timer1 interrupt at ~300.48 Hz.
    // Toggling outputs each interrupt gives ~150.24 Hz square wave.

    noInterrupts();

    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1  = 0;

    OCR1A = 207;

    // CTC mode
    TCCR1B |= _BV(WGM12);

    // Prescaler = 256
    TCCR1B |= _BV(CS12);

    // Enable compare-A interrupt
    TIMSK1 |= _BV(OCIE1A);

    interrupts();
}

ISR(TIMER1_COMPA_vect)
{
    // Toggle both:
    // D12 = PB4
    // D13 = PB5
    //
    // Writing a 1 to PINB toggles the corresponding PORTB output.
    PINB = _BV(PB4) | _BV(PB5);
}

void loop()
{
}
