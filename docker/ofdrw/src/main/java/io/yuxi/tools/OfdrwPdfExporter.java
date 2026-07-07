package io.yuxi.tools;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import org.ofdrw.converter.ConvertHelper;

public final class OfdrwPdfExporter {
    private OfdrwPdfExporter() {
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.err.println("usage: OfdrwPdfExporter <input.ofd> <output.pdf>");
            System.exit(2);
        }

        Path inputPath = Paths.get(args[0]);
        Path outputPath = Paths.get(args[1]);
        Path outputParent = outputPath.getParent();
        if (outputParent != null) {
            Files.createDirectories(outputParent);
        }

        try (InputStream input = Files.newInputStream(inputPath);
             OutputStream output = Files.newOutputStream(outputPath)) {
            ConvertHelper.toPdf(input, output);
        }
    }
}
