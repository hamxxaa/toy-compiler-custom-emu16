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
bool run_20k_instruction();
void initialize_cpu();