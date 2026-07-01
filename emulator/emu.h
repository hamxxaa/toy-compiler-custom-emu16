#include <cstdint>

typedef union reg
{
    uint16_t word;
    struct
    {
        uint8_t lower;
        uint8_t upper;
    };
} reg;

struct cpu
{
    bool running;
    reg registers[8];        // R0-R6: general purpose, R7: stack pointer (reserved)
    uint8_t memory[65536];   // 2^16 bytes of memory
    uint16_t pc;
    uint16_t flags;          // zero, sign, carry flags stored in bits 0, 1, and 2 respectively
    // NOTE: Stack pointer is now stored in registers[7].word
};

extern cpu cpu_instance;
bool run_frame_instructions();
void initialize_cpu();
void emu_set_syscall_handler(void (*fn)(uint16_t));
bool emu_present_pending();   // true if the last frame ended on a PRESENT syscall (frame yield)
void emu_begin_frame();       // resume the CPU if the previous frame PRESENTed; call before each host frame
void emu_request_present();   // yield the frame from a syscall handler (e.g. sys_ppu_submit ending in PRESENT)

// Executed-instruction counter (no-op / returns 0 unless built with -DEMU_COUNT_INSTRUCTIONS).
void emu_reset_instruction_count();
uint32_t emu_instruction_count();