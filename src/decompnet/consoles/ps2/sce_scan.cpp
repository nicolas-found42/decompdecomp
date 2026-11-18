#include "ps2recomp/sce_symbol_scanner.h"
#include "ps2recomp/types.h"
#include <fstream>
#include <iostream>
#include <iterator>

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) return 2;
    std::vector<uint8_t> data{std::istreambuf_iterator<char>(input), {}};
    ps2recomp::Section code{};
    code.name = ".text";
    code.isCode = true;
    code.address = std::stoul(argv[2], nullptr, 0);
    code.data = data.data();
    code.size = data.size();
    ps2recomp::SceSymbolScanner scanner;
    if (!scanner.loadDatabase("")) {
        std::cerr << scanner.lastError();
        return 1;
    }
    std::cout << "address\tbytes\tname\tlibrary\thash\tvariant\n";
    for (const auto &hit : scanner.scan({code})) {
        std::cout << std::hex << hit.address << '\t' << std::dec << hit.size << '\t' << hit.name
                  << '\t' << hit.library << '\t' << hit.hash << '\t' << std::hex << hit.variantHash << '\n';
    }
}
