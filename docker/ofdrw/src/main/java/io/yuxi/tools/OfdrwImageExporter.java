package io.yuxi.tools;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.List;
import org.ofdrw.converter.export.ImageExporter;

public final class OfdrwImageExporter {
    private static final String IMAGE_FORMAT = "PNG";
    private static final double IMAGE_PPM = 20D;

    private OfdrwImageExporter() {
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.err.println("usage: OfdrwImageExporter <input.ofd> <output-dir>");
            System.exit(2);
        }

        Path inputPath = Paths.get(args[0]);
        Path outputDir = Paths.get(args[1]);
        Files.createDirectories(outputDir);

        try (ImageExporter exporter = new ImageExporter(inputPath, outputDir, IMAGE_FORMAT, IMAGE_PPM)) {
            exporter.export();
            renameImages(exporter.getImgFilePaths(), outputDir);
        }
    }

    private static void renameImages(List<Path> exportedPaths, Path outputDir) throws IOException {
        for (int index = 0; index < exportedPaths.size(); index++) {
            Path exportedPath = exportedPaths.get(index);
            String extension = getExtension(exportedPath);
            Path normalizedPath = outputDir.resolve(String.format("page_%04d%s", index + 1, extension));
            Files.move(exportedPath, normalizedPath, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private static String getExtension(Path path) {
        String fileName = path.getFileName().toString();
        int dotIndex = fileName.lastIndexOf('.');
        if (dotIndex < 0) {
            return ".png";
        }
        return fileName.substring(dotIndex).toLowerCase();
    }
}
