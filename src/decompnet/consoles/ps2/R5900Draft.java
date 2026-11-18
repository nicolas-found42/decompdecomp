//@category Decompilation
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.nio.file.Files;
import java.nio.file.Path;

public class R5900Draft extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Address address = toAddr(Long.decode(args[0]));
        Function function = getFunctionAt(address);
        if (function == null) {
            disassemble(address);
            function = createFunction(address, null);
        }
        if (function == null) throw new Exception("No function starts at " + address);
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) throw new Exception(decompiler.getLastMessage());
            DecompileResults result = decompiler.decompileFunction(function, 45, monitor);
            if (!result.decompileCompleted()) throw new Exception(result.getErrorMessage());
            Files.writeString(Path.of(args[1]), result.getDecompiledFunction().getC());
        } finally {
            decompiler.dispose();
        }
    }
}
