<?php

namespace App\Services;

use Illuminate\Support\Facades\Process;
use RuntimeException;

class CotasPythonService
{
    public function analyze(
        string $pdfPath,
        string $client,
        string $partNumber,
        string $drawingNumber,
        string $revision
    ): array {
        $result = Process::timeout(180)->run([
            config('services.cotas.python', 'python'),
            base_path('tools/cotas_engine.py'),
            '--storage',
            storage_path('app/cotas'),
            'analyze',
            $pdfPath,
            '--client',
            $client,
            '--part-number',
            $partNumber,
            '--drawing-number',
            $drawingNumber,
            '--revision',
            $revision,
        ]);

        if (! $result->successful()) {
            throw new RuntimeException($result->errorOutput() ?: 'Python analysis failed.');
        }

        return json_decode($result->output(), true, flags: JSON_THROW_ON_ERROR);
    }
}
